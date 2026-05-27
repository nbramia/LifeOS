#!/usr/bin/env python3
"""Re-poll Anthropic for every managed session and rewrite stored token totals
+ total_dollars from the authoritative live usage.

Why this exists
---------------
Two compounding bugs left historical `total_dollars` numbers ~20× too low:

1. The cache-token-tracking commit (fcaeff5) landed 2026-05-27 02:17; the
   worker ran old code (which never recorded cache_creation / cache_read)
   until restarting at 18:42. Every session that ran in that window
   silently dropped its cache-token usage.
2. Live API responses use a nested `cache_creation` object
   (`{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`), not the flat
   `cache_creation_input_tokens` field the parser looked for. So even
   sessions that ran on the post-fcaeff5 code still booked $0 of
   cache_creation cost. The parser fix in this same change handles both
   shapes going forward.

This script re-polls Anthropic for every session with a
`managed_agent_session_id` and overwrites the four token counters +
`total_dollars` with the recomputed values. Wall-time overhead is
included.

Usage
-----
    # Preview (default — no writes)
    python scripts/backfill_managed_session_costs.py

    # Apply
    python scripts/backfill_managed_session_costs.py --execute

Reads ANTHROPIC_API_KEY from .env or env. Skips sessions whose remote
returns 404 (deleted), leaving their existing numbers untouched.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# .env load — match the pattern other backfill scripts use.
try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

from api.services.agent_worker.managed_driver import ManagedAgentsDriver, managed_session_cost  # noqa: E402
from config.settings import settings  # noqa: E402


DB_PATH = REPO_ROOT / "data" / "agent_sessions.db"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true", help="Apply changes (default: dry-run)")
    ap.add_argument("--model", default=None,
                    help="Pricing model to use for recompute (default: settings.agent_managed_model)")
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return 1
    model = args.model or settings.agent_managed_model

    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT task_id, session_id, managed_agent_session_id,
                  started_at, last_activity_at,
                  total_input_tokens, total_output_tokens,
                  total_cache_creation_tokens, total_cache_read_tokens,
                  total_dollars
           FROM sessions
           WHERE routing = 'claude' AND managed_agent_session_id IS NOT NULL
           ORDER BY started_at"""
    ).fetchall()

    print(f"{'DRY-RUN' if not args.execute else 'EXECUTE'}: {len(rows)} managed sessions, model={model}\n")
    print(f"{'sess':24}  {'in':>6} {'out':>7} {'cache_cr':>9} {'cache_rd':>10} {'$ before':>10} {'$ after':>10}  status")

    driver = ManagedAgentsDriver(api_key=api_key)
    try:
        sum_before = 0.0
        sum_after = 0.0
        updated = 0
        skipped = 0
        for row in rows:
            sum_before += float(row["total_dollars"] or 0)
            remote_id = row["managed_agent_session_id"]
            try:
                state = driver.get_session_state(remote_id)
            except Exception as exc:
                print(f"{remote_id:24}  {'?':>6} {'?':>7} {'?':>9} {'?':>10} {row['total_dollars']:>10.4f} {'?':>10}  poll-error: {exc}")
                sum_after += float(row["total_dollars"] or 0)
                skipped += 1
                continue

            if state.status == "cancelled" and not state.total_input_tokens:
                # 404 or deleted-remote — preserve existing row.
                print(f"{remote_id:24}  {'-':>6} {'-':>7} {'-':>9} {'-':>10} {row['total_dollars']:>10.4f} {'-':>10}  deleted-remote (kept)")
                sum_after += float(row["total_dollars"] or 0)
                skipped += 1
                continue

            wall_seconds = max(0.0, float(row["last_activity_at"] or 0) - float(row["started_at"] or 0))
            new_dollars = managed_session_cost(
                model,
                state.total_input_tokens,
                state.total_output_tokens,
                wall_seconds,
                cache_creation_tokens=state.total_cache_creation_tokens,
                cache_read_tokens=state.total_cache_read_tokens,
            )
            sum_after += new_dollars
            print(
                f"{remote_id:24}  "
                f"{state.total_input_tokens:>6} {state.total_output_tokens:>7} "
                f"{state.total_cache_creation_tokens:>9} {state.total_cache_read_tokens:>10} "
                f"{(row['total_dollars'] or 0):>10.4f} {new_dollars:>10.4f}  ok"
            )
            if args.execute:
                conn.execute(
                    """UPDATE sessions SET
                          total_input_tokens          = ?,
                          total_output_tokens         = ?,
                          total_cache_creation_tokens = ?,
                          total_cache_read_tokens     = ?,
                          total_dollars               = ?
                       WHERE task_id = ?""",
                    (
                        state.total_input_tokens,
                        state.total_output_tokens,
                        state.total_cache_creation_tokens,
                        state.total_cache_read_tokens,
                        new_dollars,
                        row["task_id"],
                    ),
                )
                updated += 1

        if args.execute:
            conn.commit()

        print(f"\nTotals: ${sum_before:.4f} → ${sum_after:.4f}  (delta +${sum_after - sum_before:.4f})")
        print(f"Sessions updated: {updated},  skipped: {skipped}")
        if not args.execute:
            print("\nRe-run with --execute to apply.")
    finally:
        driver.close()
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
