#!/usr/bin/env python3
"""Read social and contact columns from Dex or Notion CSV exports into the CRM.

Dry run by default; prints aggregate counts only.

    python scripts/import_contact_exports.py --source notion export.csv
    python scripts/import_contact_exports.py --source dex export.csv --execute
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.contact_export_import import import_contact_export  # noqa: E402
from api.services.person_entity import get_person_entity_store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path, help="CSV export files")
    parser.add_argument("--source", required=True, choices=("dex", "notion"))
    parser.add_argument("--execute", action="store_true", help="Write changes (default: dry run)")
    args = parser.parse_args()

    store = get_person_entity_store()
    for path in args.paths:
        stats = import_contact_export(path, args.source, store, execute=args.execute)
        print(json.dumps({"mode": "execute" if args.execute else "dry-run", **stats}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
