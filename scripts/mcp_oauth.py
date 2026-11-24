#!/usr/bin/env python3
"""Open app registration, and list or revoke apps connected to the MCP HTTP transport over OAuth.

    python scripts/mcp_oauth.py open [DURATION]   # 90s, 10m, 1h; default 10m, max 1h
    python scripts/mcp_oauth.py close
    python scripts/mcp_oauth.py list
    python scripts/mcp_oauth.py revoke <client_id>
    python scripts/mcp_oauth.py prune

A new app can register only while the registration window is open; it is
closed by default and closes by itself when the duration ends. Apps that are
already registered keep working either way.

Reads the OAuth store (`mcp_oauth.db` in the data directory) directly, so it
works whether or not the MCP HTTP service is running. Revoking a client
invalidates every access and refresh token it holds immediately.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.services.mcp_oauth import OAuthStore  # noqa: E402


def default_db_path() -> Path:
    from config.settings import settings

    return Path(settings.chroma_path).parent / "mcp_oauth.db"


def _fmt(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


DEFAULT_OPEN_SECONDS = 600
MAX_OPEN_SECONDS = 3600
_DURATION = re.compile(r"^(\d+)([smh])$")


def parse_duration(text: str) -> int:
    """Seconds for `90s`, `10m` or `1h`. Raises ValueError with a message for anything else."""
    match = _DURATION.match(text.strip().lower())
    if not match:
        raise ValueError(f"invalid duration {text!r}; use a form like 90s, 10m or 1h")
    seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]
    if seconds <= 0:
        raise ValueError("duration must be greater than zero")
    if seconds > MAX_OPEN_SECONDS:
        raise ValueError("duration must be at most 1h")
    return seconds


def cmd_open(store: OAuthStore, duration: str) -> int:
    try:
        seconds = parse_duration(duration)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    until = store.open_registration(seconds)
    print(f"Registration open until {_fmt(until)}.")
    return 0


def cmd_close(store: OAuthStore) -> int:
    store.close_registration()
    print("Registration closed.")
    return 0


def cmd_list(store: OAuthStore) -> int:
    until = store.registration_open_until()
    print(f"Registration: open until {_fmt(until)}" if until else "Registration: closed")
    clients = store.list_clients()
    if not clients:
        print("No registered apps.")
        return 0
    for c in clients:
        status = "revoked" if c["revoked_at"] else ("approved" if c["approved_at"] else "pending")
        print(
            f"{c['client_id']}  {c['client_name']!r}  [{status}]  "
            f"redirects={','.join(c['redirect_hosts'])}  active_grants={c['active_grants']}  "
            f"registered={_fmt(c['created_at'])}  last_used={_fmt(c['last_used_at'])}"
        )
    return 0


def cmd_revoke(store: OAuthStore, client_id: str) -> int:
    if not store.revoke_client(client_id):
        print(f"Unknown client: {client_id}", file=sys.stderr)
        return 1
    print(f"Revoked {client_id} and every token it held.")
    return 0


def cmd_prune(store: OAuthStore) -> int:
    print(f"Deleted {store.prune()} expired unapproved registration(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=None, help="OAuth store path (default: data dir mcp_oauth.db)")
    sub = parser.add_subparsers(dest="command", required=True)
    open_cmd = sub.add_parser("open", help="Allow app registration for a while (default 10m, max 1h)")
    open_cmd.add_argument("duration", nargs="?", default="10m")
    sub.add_parser("close", help="Close app registration now")
    sub.add_parser("list", help="List registered apps")
    revoke = sub.add_parser("revoke", help="Revoke an app and all its tokens")
    revoke.add_argument("client_id")
    sub.add_parser("prune", help="Delete expired unapproved registrations")
    args = parser.parse_args(argv)

    store = OAuthStore(args.db or default_db_path())
    if args.command == "open":
        return cmd_open(store, args.duration)
    if args.command == "close":
        return cmd_close(store)
    if args.command == "list":
        return cmd_list(store)
    if args.command == "prune":
        return cmd_prune(store)
    return cmd_revoke(store, args.client_id)


if __name__ == "__main__":
    sys.exit(main())
