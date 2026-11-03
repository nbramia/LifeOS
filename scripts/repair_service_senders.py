#!/usr/bin/env python3
"""
Hide existing people who are services or newsletters, and clear their relationship value.

The decision is api/services/service_sender.py's: generic address, name, and mail
signals, and a row with anything two-way or human on it is never hidden. Hiding
keeps the row's interactions and blocks its addresses so no sync recreates it.

Dry run by default, using read-only connections and writing only the review file.
--execute backs up the CRM database first, then applies exactly what a dry run
would report. Running again finds nothing to do. Output is aggregate counts; names
and addresses go only to the private review file.

--sample-gmail checks a few recent messages per undecided row for bulk headers,
which stored data lacks for older mail, and saves the answers to --bulk-samples.
Later runs given that file reuse them, so --execute applies what was reviewed.

--restore PERSON_ID undoes a hide and tags the row so it stays visible. It says what
came back and what the next nightly run rebuilds. --previous takes a JSON list of the
person ids an earlier run would hide, so the review can show what changed.

Usage:
    python3 scripts/repair_service_senders.py --crm-db data/crm.db --interactions-db data/interactions.db
    python3 scripts/repair_service_senders.py ... --sample-gmail --bulk-samples ~/.lifeos-private/service-filter/bulk-samples.json
    python3 scripts/repair_service_senders.py ... --bulk-samples FILE --execute
    python3 scripts/repair_service_senders.py ... --restore PERSON_ID [PERSON_ID ...]

Hiding holds on to interactions, source entities, and new mail, and consistency
verification leaves them. It removes a hidden row's inferred relationships, which
relationship discovery rebuilds from those interactions after a restore.

Afterwards rebuild derived data: scripts/sync_strengths.py --execute, then
scripts/sync_crm_to_vectorstore.py --execute.
"""
import argparse
import json
import logging
import os
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.services.gmail import GmailService  # noqa: E402
from api.services.google_auth import GoogleAccount  # noqa: E402
from api.services.person_entity import PersonEntityStore  # noqa: E402
from api.services.service_sender import (  # noqa: E402
    ONE_WAY_MIN_RECEIVED,
    SenderDecision,
    decide_senders,
    hide_service_person,
    restore_person,
)

logger = logging.getLogger(__name__)

DEFAULT_REPORT_DIR = Path.home() / ".lifeos-private" / "service-filter"
REVIEWED_CIRCLES = range(0, 6)
RANDOM_REST_SIZE = 100
SAMPLE_SEED = 20261103
NEAR_LINE_SIZE = 500
TWO_WAY_SIZE = 100
TOP_RANK = 500
MESSAGES_PER_ROW = 3
# One-way mail this close to the rule counts as near the line, signals or not.
NEAR_ONE_WAY_RECEIVED = ONE_WAY_MIN_RECEIVED // 2


def _strength(decision: SenderDecision) -> float:
    return decision.person.relationship_strength


def backup_crm(crm_db: Path) -> Path:
    """Copy the CRM database aside with SQLite's backup API, private to the owner."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = crm_db.with_name(f"{crm_db.name}.bak-service-senders-{stamp}")
    source = sqlite3.connect(str(crm_db), timeout=60.0)
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    os.chmod(target, 0o600)
    return target


def interaction_count(interactions_db: Path, person_ids: list[str]) -> int:
    conn = sqlite3.connect(f"file:{interactions_db.resolve()}?mode=ro", uri=True, timeout=60.0)
    try:
        return sum(
            conn.execute("SELECT COUNT(*) FROM interactions WHERE person_id = ?", (person_id,)).fetchone()[0]
            for person_id in person_ids
        )
    finally:
        conn.close()


def execute_decisions(decisions: Iterable[SenderDecision], crm_db: Path) -> int:
    """Hide every row the decisions say to. Returns how many."""
    store = PersonEntityStore(str(crm_db))
    hidden = 0
    for decision in decisions:
        if decision.will_hide:
            hide_service_person(store, decision)
            hidden += 1
    return hidden


# ---------------------------------------------------------------- bulk sample


def _recent_received_messages(
    interactions_db: Path, person_ids: set[str]
) -> dict[str, list[tuple[str, str]]]:
    """Each person's latest received Gmail messages, as (account, message id)."""
    conn = sqlite3.connect(f"file:{interactions_db.resolve()}?mode=ro", uri=True, timeout=60.0)
    messages: dict[str, list[tuple[str, str]]] = defaultdict(list)
    try:
        for person_id, account, message_id in conn.execute(
            "SELECT person_id, source_account, source_id FROM interactions "
            "WHERE source_type = 'gmail' AND substr(title, 1, 1) = '←' AND source_account IS NOT NULL "
            "ORDER BY timestamp DESC"
        ):
            if person_id in person_ids and len(messages[person_id]) < MESSAGES_PER_ROW:
                messages[person_id].append((account, message_id))
    finally:
        conn.close()
    return messages


def _fetch_from_gmail(account: str, message_ids: list[str]) -> dict[str, bool]:
    """Whether each message carries bulk headers."""
    emails = GmailService(account_type=GoogleAccount(account)).get_messages_batch(message_ids, include_body=False)
    return {message_id: email.bulk for message_id, email in emails.items()}


def sample_bulk_headers(
    decisions: Iterable[SenderDecision],
    interactions_db: Path,
    existing: Mapping[str, Mapping[str, int]],
    fetch: Callable[[str, list[str]], Mapping[str, bool]] = _fetch_from_gmail,
) -> dict[str, dict[str, int]]:
    """Check recent mail for rows the stored signals left undecided and unprotected."""
    samples = {person_id: dict(sample) for person_id, sample in existing.items()}
    undecided = {
        d.person.id for d in decisions
        if not d.verdict.is_service and not d.protected_by and d.person.id not in samples
    }
    messages = _recent_received_messages(interactions_db, undecided)
    by_account: dict[str, list[str]] = defaultdict(list)
    for rows in messages.values():
        for account, message_id in rows:
            by_account[account].append(message_id)
    answers: dict[str, bool] = {}
    for account, message_ids in by_account.items():
        try:
            answers.update(fetch(account, message_ids))
        except Exception as e:
            logger.warning("Could not sample Gmail account %s: %s", account, type(e).__name__)
    for person_id, rows in messages.items():
        found = [answers[message_id] for _, message_id in rows if message_id in answers]
        if found:
            samples[person_id] = {"checked": len(found), "bulk": sum(found)}
    return samples


def load_samples(path: Optional[Path]) -> dict:
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text())


def _write_private(path: Path, text: str) -> None:
    """Write private data. Refuses a path inside the repository."""
    resolved = path.resolve()
    if REPO_ROOT in resolved.parents:
        raise SystemExit("This file holds private data and must stay outside the repository")
    resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(resolved), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as f:
        f.write(text)
    os.chmod(resolved, 0o600)


# --------------------------------------------------------------------- report


def top_rank_strength(crm_db: Path) -> float:
    """The strength it takes to rank in the top of everyone visible, email or not."""
    conn = sqlite3.connect(f"file:{crm_db.resolve()}?mode=ro", uri=True, timeout=60.0)
    try:
        row = conn.execute(
            "SELECT relationship_strength FROM person_entities WHERE hidden = 0 "
            "ORDER BY relationship_strength DESC LIMIT 1 OFFSET ?",
            (TOP_RANK - 1,),
        ).fetchone()
    finally:
        conn.close()
    return (row[0] or 0.0) if row else 0.0


def summarize(decisions: list[SenderDecision], top_strength: float) -> dict:
    """Aggregate counts, safe to print."""
    hides = [d for d in decisions if d.will_hide]
    protected = [d for d in decisions if d.verdict.is_service and d.protected_by]
    return {
        "people_with_email": len(decisions),
        "would_hide": len(hides),
        "by_circle": dict(sorted(Counter(d.person.dunbar_circle for d in hides).items(), key=lambda kv: (kv[0] is None, kv[0]))),
        "in_top_500_by_strength": sum(_strength(d) >= top_strength for d in hides),
        "by_rule": dict(Counter(d.verdict.reason for d in hides).most_common()),
        "service_shaped_but_protected": len(protected),
        "protected_by": dict(Counter(reason for d in protected for reason in d.protected_by).most_common()),
        "protected_by_only_this": dict(Counter(d.protected_by[0] for d in protected if len(d.protected_by) == 1).most_common()),
    }


def near_line(decisions: list[SenderDecision]) -> tuple[list[SenderDecision], list[SenderDecision]]:
    """
    Kept rows closest to being hidden: service-shaped rows only protection saved, the
    thinnest protection first, then rows with a signal or one-way mail short of a rule.
    """
    protected = sorted(
        (d for d in decisions if d.verdict.is_service and d.protected_by),
        key=lambda d: (len(d.protected_by), -_strength(d)),
    )
    short = sorted(
        (
            d for d in decisions
            if not d.verdict.is_service and not d.protected_by and (
                d.verdict.signals or (d.received >= NEAR_ONE_WAY_RECEIVED and not d.thread and not d.sent)
            )
        ),
        key=lambda d: -d.received,
    )
    return protected, short


def _row(decision: SenderDecision, column: Callable[[SenderDecision], str]) -> str:
    addresses = ", ".join(a for a, _ in Counter(decision.addresses).most_common(3))
    cells = [
        str(decision.person.dunbar_circle if decision.person.dunbar_circle is not None else "-"),
        f"{_strength(decision):.1f}",
        decision.person.canonical_name.replace("|", "/"),
        addresses.replace("|", "/"),
        f"{decision.received} in, {decision.thread} thread, {decision.sent} sent",
        column(decision),
        decision.person.id,
    ]
    return "| " + " | ".join(cells) + " |"


_HEADER = (
    "| Circle | Strength | Name | Addresses | Mail | {} | Person id |\n"
    "|---|---|---|---|---|---|---|\n"
)


def _signals(decision: SenderDecision) -> str:
    return f"**{decision.verdict.reason}**: " + ", ".join(decision.verdict.signals)


def _kept_because(decision: SenderDecision) -> str:
    return ", ".join(decision.protected_by) + " (service-shaped: " + decision.verdict.reason + ")"


def _short_because(decision: SenderDecision) -> str:
    return f"{decision.verdict.reason}: " + ", ".join(decision.verdict.signals)


def build_review(
    decisions: list[SenderDecision],
    samples: Mapping[str, Mapping[str, int]],
    top_strength: float,
    previous_hides: Optional[set[str]] = None,
) -> str:
    """The private review: both error directions, with the signals that fired."""
    summary = summarize(decisions, top_strength)
    hides = sorted((d for d in decisions if d.will_hide), key=_strength, reverse=True)
    reviewed = [d for d in hides if d.person.dunbar_circle in REVIEWED_CIRCLES]
    rest = [d for d in hides if d.person.dunbar_circle not in REVIEWED_CIRCLES]
    picked = random.Random(SAMPLE_SEED).sample(rest, min(RANDOM_REST_SIZE, len(rest)))
    protected, short = near_line(decisions)
    most_two_way = sorted(
        (d for d in hides if d.sent or d.thread), key=lambda d: (-(d.sent + d.thread), -d.received),
    )[:TWO_WAY_SIZE]

    changes: list[str] = []
    if previous_hides is not None:
        new = [d for d in hides if d.person.id not in previous_hides]
        kept_now = [d for d in decisions if not d.will_hide and d.person.id in previous_hides]
        changes = [
            f"## Newly hidden compared with the last version ({len(new)})",
            "",
            _HEADER.format("Signals").rstrip(),
            *[_row(d, _signals) for d in new],
            "",
            f"## Hidden last version, kept now ({len(kept_now)})",
            "",
            _HEADER.format("Why").rstrip(),
            *[_row(d, _kept_because if d.protected_by else _short_because) for d in kept_now],
            "",
        ]

    lines = [
        "# Service and newsletter filter: review",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat()}. Bulk headers sampled for {len(samples)} rows.",
        "",
        "```json",
        json.dumps(summary, indent=2, default=str),
        "```",
        "",
        *changes,
        f"## Hidden rows with the most two-way mail, where a real person would be at risk (showing {len(most_two_way)})",
        "",
        _HEADER.format("Signals").rstrip(),
        *[_row(d, _signals) for d in most_two_way],
        "",
        f"## Would hide, circles 0-5 ({len(reviewed)})",
        "",
        _HEADER.format("Signals").rstrip(),
        *[_row(d, _signals) for d in reviewed],
        "",
        f"## Would hide, random {len(picked)} of the other {len(rest)}",
        "",
        _HEADER.format("Signals").rstrip(),
        *[_row(d, _signals) for d in picked],
        "",
        f"## Kept: service-shaped, only protection saved them ({len(protected)}, thinnest first, showing {min(NEAR_LINE_SIZE, len(protected))})",
        "",
        _HEADER.format("Kept because").rstrip(),
        *[_row(d, _kept_because) for d in protected[:NEAR_LINE_SIZE]],
        "",
        f"## Kept: signals or one-way mail short of a rule ({len(short)}, most mail first, showing {min(NEAR_LINE_SIZE, len(short))})",
        "",
        _HEADER.format("Rule and signals").rstrip(),
        *[_row(d, _short_because) for d in short[:NEAR_LINE_SIZE]],
        "",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------- main


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Hide services and newsletters that became people")
    parser.add_argument("--crm-db", required=True, type=Path)
    parser.add_argument("--interactions-db", required=True, type=Path)
    parser.add_argument("--merged-ids", type=Path, help="Default: merged_person_ids.json beside the CRM database")
    parser.add_argument("--report", type=Path, help="Private review path, outside the repository")
    parser.add_argument("--bulk-samples", type=Path, help="Private JSON of sampled bulk headers, read and written")
    parser.add_argument("--sample-gmail", action="store_true", help="Check recent mail of undecided rows")
    parser.add_argument("--execute", action="store_true", help="Back up, then apply")
    parser.add_argument("--restore", nargs="+", metavar="PERSON_ID", help="Undo the hide on these rows and stop")
    parser.add_argument("--previous", type=Path, help="JSON list of the person ids an earlier run would hide")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    if args.restore:
        backup_crm(args.crm_db)
        store = PersonEntityStore(str(args.crm_db))
        restored = [
            person for person_id in args.restore
            if (person := restore_person(store, person_id)) is not None
        ]
        print(f"Restored {len(restored)} of {len(args.restore)} rows.")
        print(f"Back now: {interaction_count(args.interactions_db, [p.id for p in restored])} interactions, "
              "including mail received while hidden, plus source entities and unblocked addresses.")
        print("Rebuilt by the next nightly run: inferred relationships, strength and circle, people index.")
        print("Not recovered: iMessage, WhatsApp, or phone contact while hidden, "
              "which those syncs don't record for a hidden row.")
        return 0

    merged_ids_path = args.merged_ids or args.crm_db.with_name("merged_person_ids.json")
    samples = load_samples(args.bulk_samples)
    decisions = decide_senders(args.crm_db, args.interactions_db, merged_ids_path, samples)
    if args.sample_gmail:
        samples = sample_bulk_headers(decisions, args.interactions_db, samples)
        if args.bulk_samples:
            _write_private(args.bulk_samples, json.dumps(samples))
        decisions = decide_senders(args.crm_db, args.interactions_db, merged_ids_path, samples)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = args.report or DEFAULT_REPORT_DIR / f"review-{stamp}.md"
    top_strength = top_rank_strength(args.crm_db)
    previous = set(json.loads(args.previous.read_text())) if args.previous else None
    _write_private(report_path, build_review(decisions, samples, top_strength, previous))

    print("EXECUTE" if args.execute else "DRY RUN (use --execute to apply)")
    print(json.dumps(summarize(decisions, top_strength), indent=2, default=str))
    print(f"Private review: {report_path}")

    if args.execute:
        backup = backup_crm(args.crm_db)
        print(f"Backed up the CRM database to {backup.name}")
        print(f"Hidden: {execute_decisions(decisions, args.crm_db)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
