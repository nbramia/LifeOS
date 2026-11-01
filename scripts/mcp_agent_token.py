#!/usr/bin/env python3
"""Mint a bearer token for an external agent on the MCP HTTP transport.

    python scripts/mcp_agent_token.py new <agent>

Prints a new random token and the `LIFEOS_MCP_AGENT_TOKENS` entry that
grants it. Give the token to the agent; put only the entry (agent=sha256) in
`.env`, so the token itself is never stored on the host. The token gets the
connected-app tool tier and `lifeos_task_update` as that agent, never the
full catalog. Nothing is written anywhere.
"""
from __future__ import annotations

import argparse
import hashlib
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.services.agent_board import EXTERNAL_AGENT_ASSIGNEES  # noqa: E402


def mint(agent: str) -> tuple[str, str]:
    """A new token and its `agent=sha256` entry."""
    token = secrets.token_urlsafe(32)
    return token, f"{agent}={hashlib.sha256(token.encode('utf-8')).hexdigest()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="mint a token for an agent")
    new.add_argument("agent", choices=EXTERNAL_AGENT_ASSIGNEES)
    args = parser.parse_args(argv)

    token, entry = mint(args.agent)
    print(f"Token for {args.agent} (shown once; give it to the agent):")
    print(f"  {token}")
    print("Add this entry to LIFEOS_MCP_AGENT_TOKENS (comma-separated), then restart lifeos-mcp-http:")
    print(f"  {entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
