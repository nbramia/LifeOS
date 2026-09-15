#!/usr/bin/env python3
"""
Two-step, non-interactive eero login (#1081).

eero's login is a verification-code flow, so it can't be automated headless
end to end — but it doesn't need a TTY either. Run once by the operator, in
two steps, from any non-interactive shell (including Claude Code's `!`
prefix, which has no TTY for a prompt):

    python scripts/eero_login.py --login you@example.com
    # ... a code arrives by SMS/email ...
    python scripts/eero_login.py --code 123456

Step one requests a verification code and stores the temporary token in the
gitignored state file (data/home/eero_session.json). Step two verifies the
code against that token and promotes it to a persistent session, still in
the same file (mode 0600) — the same file api/services/home/eero.py reads
at request time and rewrites whenever the session auto-refreshes.

Never prints the token.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402

from api.services.home import eero  # noqa: E402


async def do_login(login: str) -> int:
    async with eero._new_http_client() as client:
        try:
            resp = await client.post("/2.2/login", json={"login": login})
        except httpx.HTTPError as e:
            print(f"Login request failed ({type(e).__name__}): {e}", file=sys.stderr)
            return 1
    if resp.status_code != 200:
        print(f"Login request failed: HTTP {resp.status_code}", file=sys.stderr)
        return 1
    try:
        token = resp.json().get("data", {}).get("user_token")
    except ValueError:
        token = None
    if not token:
        print("Login response did not include a user_token", file=sys.stderr)
        return 1
    eero._save_token(token)
    print("Verification code requested. Run --code <code> once it arrives.")
    return 0


async def do_verify(code: str) -> int:
    pending = eero._load_token()
    if not pending:
        print(
            "No pending token found — run --login <email-or-phone> first.",
            file=sys.stderr,
        )
        return 1
    async with eero._new_http_client() as client:
        try:
            resp = await client.post(
                "/2.2/login/verify",
                json={"code": code},
                headers={"Cookie": f"s={pending}"},
            )
        except httpx.HTTPError as e:
            print(f"Verification failed ({type(e).__name__}): {e}", file=sys.stderr)
            return 1
    if resp.status_code != 200:
        print(f"Verification failed: HTTP {resp.status_code}", file=sys.stderr)
        return 1
    try:
        body = resp.json()
    except ValueError:
        body = {}
    # A promoted token may come back in the response, or the vendor may
    # simply mark the existing cookie persistent — keep whichever token we
    # actually have evidence for.
    persistent = (body.get("data") or {}).get("user_token") or pending
    eero._save_token(persistent)
    print(f"Session verified and saved to {eero.STATE_PATH}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--login", metavar="EMAIL_OR_PHONE", help="Step 1: request a verification code")
    group.add_argument("--code", metavar="CODE", help="Step 2: verify the code and persist the session")
    args = parser.parse_args()

    if args.login:
        return asyncio.run(do_login(args.login))
    return asyncio.run(do_verify(args.code))


if __name__ == "__main__":
    sys.exit(main())
