"""
Past organizations for a person.

A person's `company` and `position` are the current organization and role. This module owns
the list of organizations they were with before. Each entry is a dict:

    name    The organization's name
    source  "linkedin_profile" (a job with an end date on a scraped LinkedIn profile) or
            "manual" (entered by hand, never overwritten by the backfill)
    since   First date at the organization ("YYYY", "YYYY-MM" or "YYYY-MM-DD"), or None
    until   Last date there, in the same form, or None

Nothing else can add an entry: `clean_entry` rejects any other source. An organization that
matches the person's current company, or a job still open on their profile, is never past.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Iterable, Optional

SOURCE_PROFILE = "linkedin_profile"
SOURCE_MANUAL = "manual"
SOURCES = frozenset({SOURCE_PROFILE, SOURCE_MANUAL})

MAX_PAST_ORGANIZATIONS = 10
MAX_NAME_LENGTH = 120

_DATE_PART = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_WEB_ADDRESS = re.compile(r"(https?://|www\.)", re.IGNORECASE)
_LEGAL_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "corp", "corporation",
    "co", "company", "plc", "gmbh", "sa", "ag", "pbc",
})
_NON_ORGANIZATIONS = frozenset({
    "selfemployed", "freelance", "freelancer", "independent", "independentconsultant",
    "stealthstartup", "stealth", "confidential", "unemployed", "none", "na",
})


def org_key(name: Optional[str]) -> str:
    """A comparison key for an organization name: case, punctuation and legal suffixes ignored."""
    text = (name or "").casefold().replace("&", " and ").replace(".", "").replace("'", "").replace("’", "")
    words = re.findall(r"[^\W_]+", text)
    if words and words[0] == "the":
        words = words[1:]
    while len(words) > 1 and (words[-1] in _LEGAL_SUFFIXES or words[-1] == "and"):
        words.pop()
    return " ".join(words)


def same_org(first: Optional[str], second: Optional[str]) -> bool:
    """Whether two names could be one organization: equal keys, or one is the other plus more words."""
    a, b = org_key(first).split(), org_key(second).split()
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer[: len(shorter)] == shorter


def is_organization_name(name: Optional[str]) -> bool:
    """Whether a name is an organization: not blank, a placeholder such as "self-employed", or a web address."""
    key = org_key(name).replace(" ", "")
    return bool(key) and key not in _NON_ORGANIZATIONS and not _WEB_ADDRESS.match((name or "").strip())


def _date_part(value: object) -> Optional[str]:
    return value.strip() if isinstance(value, str) and _DATE_PART.match(value.strip()) else None


def entry_error(raw: object) -> Optional[str]:
    """Why a hand-entered entry cannot be stored (no name, or a date in the wrong form), or None."""
    name = raw.get("name") if isinstance(raw, dict) else None
    if not isinstance(name, str) or not name.strip():
        return "name is required"
    for key in ("since", "until"):
        value = raw.get(key)
        if value not in (None, "") and _date_part(value) is None:
            return f"{key} must be YYYY, YYYY-MM or YYYY-MM-DD"
    return None


def clean_entry(raw: object) -> Optional[dict]:
    """An entry in canonical form, or None when it is malformed or from another source."""
    if not isinstance(raw, dict) or raw.get("source") not in SOURCES:
        return None
    name = " ".join(str(raw.get("name") or "").split())[:MAX_NAME_LENGTH].strip()
    if not is_organization_name(name):
        return None
    return {"name": name, "source": raw["source"], "since": _date_part(raw.get("since")), "until": _date_part(raw.get("until"))}


def normalize_entries(entries: Iterable[object]) -> list[dict]:
    """
    Canonical entries, one per organization (`same_org`), latest first, capped in length.

    Two entries for one organization from the same source merge their dates; otherwise the
    first stands.
    """
    kept: list[dict] = []
    for raw in entries or []:
        entry = clean_entry(raw)
        if entry is None:
            continue
        held = next((k for k in kept if same_org(k["name"], entry["name"])), None)
        if held is None:
            kept.append(entry)
        elif held["source"] == entry["source"]:
            starts = [d for d in (held["since"], entry["since"]) if d]
            ends = [d for d in (held["until"], entry["until"]) if d]
            held["since"], held["until"] = (min(starts) if starts else None), (max(ends) if ends else None)
    kept.sort(key=lambda e: e["name"].casefold())
    kept.sort(key=lambda e: e["until"] or "", reverse=True)
    return kept[:MAX_PAST_ORGANIZATIONS]


def visible_past_organizations(entries: Iterable[object], current_company: Optional[str]) -> list[dict]:
    """The entries to show: never the organization the person works for."""
    return [e for e in normalize_entries(entries) if not same_org(e["name"], current_company)]


def format_past_organizations(entries: Iterable[object], current_company: Optional[str], limit: int = 5) -> str:
    """`Name (until 2021), Other Name`; empty when there are none to show."""
    parts = []
    for entry in visible_past_organizations(entries, current_company)[:limit]:
        year = (entry["until"] or "")[:4]
        parts.append(f"{entry['name']} (until {year})" if year else entry["name"])
    return ", ".join(parts)


def derive_profile_organizations(
    profile: dict, current_company: Optional[str] = None, today: Optional[date] = None
) -> list[dict]:
    """
    Past employers from a scraped profile's `experience` list (company, start_year, start_month,
    end_year, end_month).

    A job with no end year, or an end after `today`, is open. A company with an open job, or
    the person's current company, is not past.
    """
    today = today or datetime.now(timezone.utc).date()
    jobs = []
    for job in profile.get("experience") or []:
        if not isinstance(job, dict) or not is_organization_name(job.get("company")):
            continue
        since, until = _partial(job.get("start_year"), job.get("start_month")), _partial(job.get("end_year"), job.get("end_month"))
        is_open = until is None or (int(until[:4]), int(until[5:7] or 1) if len(until) > 4 else 1) > (today.year, today.month)
        jobs.append({"name": job["company"], "since": since, "until": until, "open": is_open})
    current = [j["name"] for j in jobs if j["open"]] + ([current_company] if current_company else [])
    return normalize_entries(
        {"name": j["name"], "source": SOURCE_PROFILE, "since": j["since"], "until": j["until"]}
        for j in jobs
        if not any(same_org(j["name"], name) for name in current)
    )


def _partial(year: object, month: object) -> Optional[str]:
    if not isinstance(year, int):
        return None
    return f"{year:04d}-{month:02d}" if isinstance(month, int) and 1 <= month <= 12 else f"{year:04d}"
