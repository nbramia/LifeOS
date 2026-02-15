#!/usr/bin/env python3
"""
Google OAuth authentication CLI.

Runs the browser-based OAuth flow to authenticate a Google account
and saves the token for LifeOS to use.

Usage:
    python scripts/google_auth.py --account personal
    python scripts/google_auth.py --account work
"""
import argparse
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.google_auth import get_google_auth, GoogleAccount


def main():
    parser = argparse.ArgumentParser(description="Google OAuth authentication for LifeOS")
    parser.add_argument(
        "--account",
        choices=["personal", "work"],
        default="personal",
        help="Account type to authenticate (default: personal)",
    )
    args = parser.parse_args()

    account = GoogleAccount.PERSONAL if args.account == "personal" else GoogleAccount.WORK

    print(f"Authenticating {args.account} Google account...")
    print("A browser window will open for Google sign-in.\n")

    try:
        auth = get_google_auth(account)
        creds = auth.get_credentials()
        print(f"\nAuthenticated successfully!")
        print(f"Token saved to: {auth.token_path}")
    except FileNotFoundError as e:
        print(f"\nError: {e}")
        print(f"\nDownload OAuth credentials from Google Cloud Console and save as:")
        print(f"  config/credentials-{args.account}.json")
        sys.exit(1)
    except Exception as e:
        print(f"\nAuthentication failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
