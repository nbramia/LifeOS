#!/usr/bin/env python3
"""
Guided first-run identity setup: owner, partner, family, work domains (#763).

Turns finding your own PersonEntity ID (previously: curl the people-search
endpoint by hand, then hand-edit config/family_members.json and
config/relationship_overrides.json) into a short interactive conversation.
Run this once, after the first vault sync/index completes, so there are
indexed people to search and pick yourself and your family from.

Usage:
    ~/.venvs/lifeos/bin/python scripts/setup_identity.py

Requires the LifeOS API server to be running (uses the same people-search
endpoint the CRM UI uses -- GET /api/crm/people?q=... -- rather than reading
the database directly). Point it at a non-default server with LIFEOS_API_URL.

Safe to run more than once:
- Existing .env / config/*.json files are merged (new values are added,
  nothing else in the file is touched), never blindly replaced.
- Every file this script is about to modify is backed up first
  (<file>.bak.<UTC timestamp>) if it already exists.
- Nothing is written until a value has actually been provided -- an
  unanswered prompt just leaves that setting alone.

This script never runs automatically -- it is only ever invoked by an
operator, by hand.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

PROJECT_DIR = Path(__file__).parent.parent
ENV_PATH = PROJECT_DIR / ".env"
ENV_EXAMPLE_PATH = PROJECT_DIR / ".env.example"
FAMILY_CONFIG_PATH = PROJECT_DIR / "config" / "family_members.json"
FAMILY_EXAMPLE_PATH = PROJECT_DIR / "config" / "family_members.example.json"
RELATIONSHIP_CONFIG_PATH = PROJECT_DIR / "config" / "relationship_overrides.json"
RELATIONSHIP_EXAMPLE_PATH = PROJECT_DIR / "config" / "relationship_overrides.example.json"

API_BASE = os.environ.get("LIFEOS_API_URL", "http://localhost:8000").rstrip("/")


# ============================================================================
# Write/merge logic -- pure filesystem operations, unit-tested against temp
# config directories. No network access, no prompts.
# ============================================================================

def backup_file(path: Path) -> Optional[Path]:
    """Copy an existing file to <path>.bak.<UTC timestamp> before it's
    modified. Returns the backup path, or None if there was nothing to back
    up (file doesn't exist yet). The timestamp includes microseconds, and a
    numeric suffix is added on top of that if a backup with that exact name
    already exists -- so two runs in quick succession never overwrite each
    other's backup of the pre-existing file."""
    if not path.exists():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = path.with_name(f"{path.name}.bak.{timestamp}")
    suffix = 2
    while backup_path.exists():
        backup_path = path.with_name(f"{path.name}.bak.{timestamp}.{suffix}")
        suffix += 1
    backup_path.write_bytes(path.read_bytes())
    return backup_path


def _atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` via a temp file + rename, so a crash or
    disk-full event mid-write can never leave `path` truncated or
    half-written -- the original file is left intact until the new one is
    fully flushed to disk. Preserves the existing file's permissions
    (.env may be chmod 600, e.g. for an API key) instead of letting the
    new file fall back to the umask default."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = path.stat().st_mode if path.exists() else None
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        tmp_path.write_text(content)
        if existing_mode is not None:
            os.chmod(tmp_path, existing_mode)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, data: dict) -> None:
    _atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def _blank_template_value(value):
    """Blank out one top-level JSON value, keeping its container type
    (list/dict/string) but dropping illustrative placeholder content like
    'uuid-of-partner' or 'smith' -- an empty list/dict/string rather than
    the template's fake sample data."""
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        return {}
    if isinstance(value, str):
        return ""
    return value


def _skeleton_from_example(example_path: Path) -> dict:
    """Build a fresh config dict with the same top-level keys as the
    .example template, but with all illustrative values blanked out -- so
    a brand-new config file starts empty, not pre-populated with the
    template's fake sample data."""
    if not example_path.exists():
        return {}
    try:
        with open(example_path) as f:
            template = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return {key: _blank_template_value(value) for key, value in template.items()}


def _existing_partner_person_id(config_path: Path) -> str:
    """Read the current partner_person_id out of relationship_overrides.json,
    if it exists, purely so main() can warn about drift -- never used to
    decide what to write."""
    if not config_path.exists():
        return ""
    try:
        with open(config_path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return ""
        return data.get("partner_person_id", "") or ""
    except (json.JSONDecodeError, OSError):
        return ""


def _quote_env_value(value: str) -> str:
    """Quote an env value if it contains characters that would otherwise
    be misread (whitespace, '#')."""
    if value == "" or re.search(r"[\s#]", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _find_env_var_line(lines: list[str], key: str) -> Optional[int]:
    """Index of an existing KEY=... line for `key`, preferring an
    uncommented assignment over a commented-out one. If a key is assigned
    more than once (a pre-existing anomaly in the file -- dotenv parsers
    apply the last assignment), the *last* match is returned, so setting
    the key here actually changes the effective value instead of updating
    a line an earlier duplicate would still shadow. None if absent."""
    active = re.compile(rf"^{re.escape(key)}=")
    last_active = None
    for i, line in enumerate(lines):
        if active.match(line):
            last_active = i
    if last_active is not None:
        return last_active
    commented = re.compile(rf"^#\s*{re.escape(key)}=")
    for i, line in enumerate(lines):
        if commented.match(line):
            return i
    return None


def write_env_updates(env_path: Path, example_path: Path, updates: dict[str, str]) -> Optional[Path]:
    """Merge `updates` (env var name -> value) into env_path, creating it
    from example_path if it doesn't exist yet. Existing assignments are
    updated in place (a commented-out one is uncommented); anything not
    already present is appended together at the end, behind a single blank
    line. Returns the backup path (or None if env_path didn't already
    exist). No-op if updates is empty."""
    if not updates:
        return None
    backup = backup_file(env_path)
    if env_path.exists():
        content = env_path.read_text()
    elif example_path.exists():
        content = example_path.read_text()
    else:
        content = ""
    lines = content.splitlines()
    to_append = []
    for key, value in updates.items():
        new_line = f"{key}={_quote_env_value(value)}"
        idx = _find_env_var_line(lines, key)
        if idx is not None:
            lines[idx] = new_line
        else:
            to_append.append(new_line)
    if to_append:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.extend(to_append)
    _atomic_write_text(env_path, "\n".join(lines) + "\n")
    return backup


def merge_family_config(
    config_path: Path,
    example_path: Path,
    family_last_names: Optional[list[str]] = None,
    family_person_ids: Optional[list[str]] = None,
) -> dict:
    """Merge new surnames/person IDs into config/family_members.json,
    creating it from the .example template's shape (with placeholder
    values blanked) if it doesn't exist. Returns a report dict with the
    backup path (if any) and the resulting config."""
    backup = backup_file(config_path)
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
    else:
        config = _skeleton_from_example(example_path)

    config.setdefault("family_last_names", [])
    config.setdefault("family_exact_names", [])
    config.setdefault("family_person_ids", [])
    config.setdefault("tracked_relationships", [])
    config.setdefault("default_selected_ids", [])

    if family_last_names:
        existing = {n.lower() for n in config["family_last_names"]}
        for name in family_last_names:
            name = name.strip()
            if name and name.lower() not in existing:
                config["family_last_names"].append(name)
                existing.add(name.lower())

    if family_person_ids:
        existing_ids = set(config["family_person_ids"])
        for person_id in family_person_ids:
            if person_id and person_id not in existing_ids:
                config["family_person_ids"].append(person_id)
                existing_ids.add(person_id)

    _atomic_write_json(config_path, config)

    return {"path": config_path, "backup": backup, "config": config}


def merge_relationship_overrides(
    config_path: Path,
    example_path: Path,
    partner_person_id: Optional[str] = None,
) -> dict:
    """Merge a partner person ID into config/relationship_overrides.json,
    creating it from the .example template's shape (with placeholder
    values blanked) if it doesn't exist. Existing strength/circle overrides
    are left untouched. Returns a report dict with the backup path (if
    any) and the resulting config."""
    backup = backup_file(config_path)
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
    else:
        config = _skeleton_from_example(example_path)

    config.setdefault("strength_overrides", {})
    config.setdefault("circle_overrides", {})
    config.setdefault("partner_person_id", "")

    if partner_person_id:
        config["partner_person_id"] = partner_person_id

    _atomic_write_json(config_path, config)

    return {"path": config_path, "backup": backup, "config": config}


def apply_identity_config(
    *,
    env_path: Path,
    env_example_path: Path,
    family_config_path: Path,
    family_example_path: Path,
    relationship_config_path: Path,
    relationship_example_path: Path,
    my_person_id: Optional[str] = None,
    partner_name: Optional[str] = None,
    partner_person_id: Optional[str] = None,
    family_last_names: Optional[list[str]] = None,
    family_person_ids: Optional[list[str]] = None,
    work_email_domain: Optional[str] = None,
    work_email_domain_2: Optional[str] = None,
    work_email_domains_extra: Optional[list[str]] = None,
) -> dict:
    """Write everything the operator provided into .env and the two config
    JSON files. Every value is optional -- an unset value is simply not
    written. Returns a report describing what changed and where backups
    (if any) were made."""
    env_updates = {}
    if my_person_id:
        env_updates["LIFEOS_MY_PERSON_ID"] = my_person_id
    if partner_name:
        env_updates["LIFEOS_PARTNER_NAME"] = partner_name
    if work_email_domain:
        env_updates["LIFEOS_WORK_DOMAIN"] = work_email_domain
    if work_email_domain_2:
        env_updates["LIFEOS_WORK_DOMAIN_2"] = work_email_domain_2
    if work_email_domains_extra:
        env_updates["LIFEOS_WORK_DOMAINS_EXTRA"] = ",".join(work_email_domains_extra)

    env_backup = write_env_updates(env_path, env_example_path, env_updates)

    family_report = None
    if family_last_names or family_person_ids:
        family_report = merge_family_config(
            family_config_path,
            family_example_path,
            family_last_names=family_last_names,
            family_person_ids=family_person_ids,
        )

    relationship_report = None
    if partner_person_id:
        relationship_report = merge_relationship_overrides(
            relationship_config_path,
            relationship_example_path,
            partner_person_id=partner_person_id,
        )

    return {
        "env_updates": env_updates,
        "env_backup": env_backup,
        "family_report": family_report,
        "relationship_report": relationship_report,
    }


# ============================================================================
# People search (HTTP -- reuses the same endpoint the CRM UI uses, never
# reads the database directly) and interactive prompts.
# ============================================================================

def count_indexed_people(api_base: str) -> int:
    """How many people the CRM currently has indexed, via the same
    endpoint the CRM UI's people list uses."""
    resp = httpx.get(f"{api_base}/api/crm/people", params={"limit": 1}, timeout=10)
    resp.raise_for_status()
    return resp.json().get("total", 0)


def search_people(api_base: str, query: str, limit: int = 10) -> list[dict]:
    """Search indexed people by name, via GET /api/crm/people?q=..."""
    resp = httpx.get(f"{api_base}/api/crm/people", params={"q": query, "limit": limit}, timeout=10)
    resp.raise_for_status()
    return resp.json().get("people", [])


def _describe_person(person: dict) -> str:
    bits = [person.get("canonical_name") or person.get("display_name") or "(unnamed)"]
    if person.get("company"):
        bits.append(person["company"])
    if person.get("emails"):
        bits.append(person["emails"][0])
    return " -- ".join(bits)


def prompt_pick_person(api_base: str, label: str, default_query: str = "") -> Optional[dict]:
    """Search-and-select loop for one person. Returns the chosen person
    dict, or None if the operator gives up (blank input at the search
    prompt)."""
    query = default_query
    while True:
        if not query:
            query = input(f"Search for {label} by name (blank to skip): ").strip()
            if not query:
                return None
        matches = search_people(api_base, query)
        if not matches:
            print(f"  No matches for '{query}'.")
            query = ""
            continue
        for i, person in enumerate(matches, 1):
            print(f"  {i}. {_describe_person(person)}")
        choice = input("  Pick a number, 's' to search again, or blank to skip: ").strip().lower()
        if choice == "":
            return None
        if choice == "s":
            query = ""
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(matches):
            return matches[int(choice) - 1]
        print("  Not a valid choice.")


def prompt_list(prompt_text: str) -> list[str]:
    raw = input(prompt_text).strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def main() -> int:
    print("LifeOS identity setup")
    print(f"Using API at {API_BASE} (override with LIFEOS_API_URL)\n")

    try:
        total = count_indexed_people(API_BASE)
    except httpx.HTTPError as e:
        print(f"Could not reach the LifeOS API at {API_BASE}: {e}")
        print("Is it running? Try: ./scripts/server.sh start")
        return 1

    if total == 0:
        print("No people are indexed yet -- run the first vault sync before this script:")
        print("  ~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --execute --force")
        return 1

    print(f"{total} people are indexed. Let's find you.\n")

    unmatched: list[str] = []

    me = prompt_pick_person(API_BASE, "yourself")
    my_person_id = me["id"] if me else None
    if me:
        print(f"You are: {_describe_person(me)}\n")
    else:
        print("Skipped -- you can re-run this script later to set your identity.\n")

    partner_name = input("Partner's first name (blank to skip): ").strip() or None
    partner_person_id = None
    if partner_name:
        partner = prompt_pick_person(API_BASE, "your partner", default_query=partner_name)
        if partner:
            partner_person_id = partner["id"]
            print(f"Partner matched: {_describe_person(partner)}\n")
        else:
            unmatched.append(partner_name)
            print(f"Could not match '{partner_name}' to an indexed person -- "
                  f"LIFEOS_PARTNER_NAME will still be set.\n")
            existing_partner_id = _existing_partner_person_id(RELATIONSHIP_CONFIG_PATH)
            if existing_partner_id:
                print(f"Note: {RELATIONSHIP_CONFIG_PATH} still has a partner_person_id "
                      f"from a previous run ({existing_partner_id}) -- it will NOT be "
                      f"changed since '{partner_name}' didn't match anyone. Re-run and "
                      f"pick a match to update it.\n")

    family_last_names = prompt_list(
        "Family surnames, comma-separated (e.g. 'Smith, Jones'; blank to skip): "
    )

    family_person_ids: list[str] = []
    family_people_names = prompt_list(
        "Any other specific family members by name, comma-separated (blank to skip): "
    )
    for name in family_people_names:
        person = prompt_pick_person(API_BASE, name, default_query=name)
        if person:
            family_person_ids.append(person["id"])
            print(f"Matched: {_describe_person(person)}\n")
        else:
            unmatched.append(name)

    domains = prompt_list(
        "Work email domain(s), comma-separated (e.g. 'acme.com, othercompany.com'; blank to skip): "
    )
    work_email_domain = domains[0] if len(domains) >= 1 else None
    work_email_domain_2 = domains[1] if len(domains) >= 2 else None
    work_email_domains_extra = domains[2:] if len(domains) > 2 else None

    report = apply_identity_config(
        env_path=ENV_PATH,
        env_example_path=ENV_EXAMPLE_PATH,
        family_config_path=FAMILY_CONFIG_PATH,
        family_example_path=FAMILY_EXAMPLE_PATH,
        relationship_config_path=RELATIONSHIP_CONFIG_PATH,
        relationship_example_path=RELATIONSHIP_EXAMPLE_PATH,
        my_person_id=my_person_id,
        partner_name=partner_name,
        partner_person_id=partner_person_id,
        family_last_names=family_last_names,
        family_person_ids=family_person_ids,
        work_email_domain=work_email_domain,
        work_email_domain_2=work_email_domain_2,
        work_email_domains_extra=work_email_domains_extra,
    )

    print("\n--- Done ---")
    if report["env_updates"]:
        print(f"Wrote to {ENV_PATH}:")
        for key, value in report["env_updates"].items():
            print(f"  {key}={value}")
        if report["env_backup"]:
            print(f"  (previous .env backed up to {report['env_backup']})")
    else:
        print("Nothing written to .env (no answers provided).")

    if report["family_report"]:
        print(f"Updated {report['family_report']['path']}")
        if report["family_report"]["backup"]:
            print(f"  (previous file backed up to {report['family_report']['backup']})")

    if report["relationship_report"]:
        print(f"Updated {report['relationship_report']['path']}")
        if report["relationship_report"]["backup"]:
            print(f"  (previous file backed up to {report['relationship_report']['backup']})")

    if unmatched:
        print("\nCould not match to an indexed person (recorded as text only, if at all):")
        for name in unmatched:
            print(f"  - {name}")

    if report["env_updates"] or report["family_report"] or report["relationship_report"]:
        print("\nRestart the server for these changes to take effect: ./scripts/server.sh restart")

    return 0


if __name__ == "__main__":
    sys.exit(main())
