#!/usr/bin/env python3
"""
Authenticate Google accounts for LifeOS.

This script initiates the OAuth flow for both personal and work accounts.
Run this once to set up authentication, then tokens will auto-refresh.

Usage:
    python scripts/authenticate_google.py [--personal] [--work] [--all]
"""
import sys
import argparse
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.google_auth import (
    GoogleAuthService,
    GoogleAccount,
    authenticate_all_accounts,
)


def main():
    parser = argparse.ArgumentParser(
        description="Authenticate Google accounts for LifeOS"
    )
    parser.add_argument(
        "--personal",
        action="store_true",
        help="Authenticate personal account (e.g., your-email@gmail.com)"
    )
    parser.add_argument(
        "--work",
        action="store_true",
        help="Authenticate work account (e.g., your-email@company.com)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Authenticate both accounts"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Check authentication status without authenticating"
    )

    args = parser.parse_args()

    # Default to --all if no specific account specified
    if not any([args.personal, args.work, args.all, args.status]):
        args.all = True

    config_dir = Path("./config")

    if args.status:
        print("\nAuthentication Status:")
        print("-" * 40)
        for account_type in GoogleAccount:
            creds_path = config_dir / f"credentials-{account_type.value}.json"
            token_path = config_dir / f"token-{account_type.value}.json"

            if not creds_path.exists():
                print(f"  {account_type.value}: No credentials file")
                continue

            service = GoogleAuthService(
                credentials_path=str(creds_path),
                token_path=str(token_path),
                account_type=account_type
            )

            if service.is_authenticated:
                print(f"  {account_type.value}: Authenticated")
            else:
                print(f"  {account_type.value}: Not authenticated")
        return

    accounts_to_auth = []
    if args.all:
        accounts_to_auth = list(GoogleAccount)
    else:
        if args.personal:
            accounts_to_auth.append(GoogleAccount.PERSONAL)
        if args.work:
            accounts_to_auth.append(GoogleAccount.WORK)

    print("\nLifeOS Google Authentication")
    print("=" * 40)

    for account_type in accounts_to_auth:
        creds_path = config_dir / f"credentials-{account_type.value}.json"
        token_path = config_dir / f"token-{account_type.value}.json"

        print(f"\n{account_type.value.upper()} Account")
        print("-" * 40)

        if not creds_path.exists():
            print(f"  ERROR: Credentials file not found at {creds_path}")
            continue

        service = GoogleAuthService(
            credentials_path=str(creds_path),
            token_path=str(token_path),
            account_type=account_type
        )

        try:
            print("  Opening browser for authentication...")
            print("  (Please authorize in the browser window)")
            credentials = service.get_credentials()
            print(f"  SUCCESS: {account_type.value} account authenticated!")
            print(f"  Token saved to: {token_path}")
        except Exception as e:
            print(f"  ERROR: {e}")

    print("\n" + "=" * 40)
    print("Authentication complete!")


if __name__ == "__main__":
    main()
