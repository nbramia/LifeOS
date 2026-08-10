#!/usr/bin/env python3
"""Re-authenticate Monarch Money and save the session.

The documented flow uses ``interactive_login()``, which prompts on stdin and so
only works from a real TTY. This variant reads the credentials already present
in ``.env`` and takes the MFA code as an argument, so it can be run from any
non-interactive shell:

    python scripts/monarch_reauth.py 123456

The MFA code is a short-lived TOTP, so run this promptly after reading it.
Verifies the saved session with a live authenticated call before declaring
success — writing the file is not proof that it works.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import dotenv_values  # noqa: E402
from monarchmoney import MonarchMoney  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent
SESSION_PATH = PROJECT_ROOT / "data" / "monarch_session.pickle"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Re-authenticate Monarch Money")
    parser.add_argument("code", help="6-digit MFA code from your authenticator")
    args = parser.parse_args()

    env = dotenv_values(PROJECT_ROOT / ".env")
    email, password = env.get("MONARCH_EMAIL"), env.get("MONARCH_PASSWORD")
    if not email or not password:
        print("MONARCH_EMAIL / MONARCH_PASSWORD missing from .env", file=sys.stderr)
        return 1

    mm = MonarchMoney()
    try:
        await mm.multi_factor_authenticate(email, password, args.code.strip())
    except Exception as e:
        print(f"MFA failed ({type(e).__name__}): {str(e)[:200]}", file=sys.stderr)
        print("If the code expired, get a fresh one and re-run.", file=sys.stderr)
        return 1

    mm.save_session(str(SESSION_PATH))

    # A saved file proves nothing — confirm the session actually authenticates.
    try:
        accounts = await mm.get_accounts()
        n = len(accounts.get("accounts", []))
    except Exception as e:
        print(f"Session saved but verification call FAILED "
              f"({type(e).__name__}): {str(e)[:200]}", file=sys.stderr)
        return 1

    print(f"Session saved to {SESSION_PATH} and verified — {n} accounts reachable.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
