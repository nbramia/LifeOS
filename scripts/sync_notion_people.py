#!/usr/bin/env python3
"""
One-way sync from LifeOS to the Notion "People" data source.

LifeOS wins. For every Notion row that maps to a LifeOS person the script sets:

  * LifeOS_ID     the person's id, when the cell is empty
  * Email, Phone  LifeOS's first value, unless the Notion value is already one
                  the person is known by (so a matching value is never rewritten)
  * Company, Title        from the person's company and position, when the
                  LifeOS value looks like a real company or job title and does
                  not lose detail Notion already holds
  * LinkedIn, Instagram, Twitter/X, Website   from linkedin_url / social_links
  * Last Check-In the local date of last_seen, only when the cell is empty or
                  older (never backwards, never in the future)

A LifeOS field with no value never blanks the Notion value. Every other
property is left alone, and no page is ever archived or deleted. Each page gets
at most two PATCH requests: the LifeOS_ID write, then one holding only the
changed properties.

Execute mode appends one JSON line per change to a ledger file (page, field,
old and new value) so any value can be restored by hand.

LifeOS people that have no Notion row and qualify (see qualifies_for_page)
become new pages, closest relationships first, using the People "Person"
template when LIFEOS_NOTION_PERSON_TEMPLATE is set. A person whose name equals
any Notion row's name is never created. A row that matches a person only by
name is reported for review and never written. Notion applies the template
asynchronously, so the remaining properties are written after its content
appears.

Dry run unless --execute. Re-running is safe: a second run finds nothing to do.
The run aborts without writing when fewer than half of the Notion rows can be
matched to a LifeOS person.

Notion row -> LifeOS person is resolved in this order:
  1. the row's LifeOS_ID equals a LifeOS person id (an id with no such person
     is an orphan, an id on two rows is a duplicate; neither is written)
  2. data/notion_people_map.json    stored mapping
  3. data/notion_people_dexmap.json seed keyed by the row's Dex_ID text
  4. identifiers                    Email, Phone (last 10 digits), LinkedIn URL;
                                    accepted only when exactly one person matches
Matches from 2 to 4 must also pass a first-and-last-name check.

Exit status: 0 success, 1 some page writes failed, 2 configuration, LifeOS API,
Notion access or unreadable-file problem, 3 aborted because the mapping looks
broken. A final SYNC_STATS line is printed on every exit path.
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import json
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, tzinfo
from typing import Any, Callable, Optional
from urllib.parse import unquote

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_API_BASE = "http://127.0.0.1:8000"
DEFAULT_MAP_PATH = PROJECT_ROOT / "data" / "notion_people_map.json"
DEFAULT_DEXMAP_PATH = PROJECT_ROOT / "data" / "notion_people_dexmap.json"
DEFAULT_LEDGER_PATH = PROJECT_ROOT / "data" / "notion_sync_ledger.jsonl"
DEFAULT_LOG_PATH = PROJECT_ROOT / "logs" / "notion-people-sync.log"

NOTION_API = "https://api.notion.com/v1"
DEFAULT_NOTION_VERSION = "2025-09-03"
PACE_SECONDS = 0.35  # about 3 requests per second, every request counted
MAX_RETRIES = 4
MAX_RETRY_WAIT = 60.0
TEMPLATE_POLL_INTERVAL = 2.0
TEMPLATE_POLL_TIMEOUT = 20.0
DEFAULT_MAX_WRITES = 100
DEFAULT_MAX_ID_WRITES = 300
DEFAULT_MAX_CREATES = 25
MIN_MATCH_RATIO = 0.5
PENDING_CHECK_RUNS = 3  # runs a create may stay pending before the log asks for a check
MAX_TEXT = 2000        # Notion's limit for one rich_text content string
MAX_VALUE_LEN = 100    # longest Company or Title value LifeOS may write
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")

PROP_NAME = "Full Name"
PROP_LIFEOS_ID = "LifeOS_ID"
PROP_DEX_ID = "Dex_ID"
PROP_EMAIL = "Email"
PROP_PHONE = "Phone"
PROP_COMPANY = "Company"
PROP_TITLE = "Title"
PROP_LINKEDIN = "LinkedIn"
PROP_INSTAGRAM = "Instagram"
PROP_TWITTER = "Twitter/X"
PROP_WEBSITE = "Website"
PROP_CHECKIN = "Last Check-In"

# Notion calls a text property "rich_text". A required property must exist with
# its type; an optional one is skipped when absent but must match when present.
REQUIRED_PROPERTIES = {
    PROP_NAME: "title",
    PROP_LIFEOS_ID: "rich_text",
    PROP_EMAIL: "email",
    PROP_PHONE: "phone_number",
    PROP_CHECKIN: "date",
    PROP_COMPANY: "rich_text",
    PROP_TITLE: "rich_text",
}
OPTIONAL_PROPERTIES = {
    PROP_LINKEDIN: "url",
    PROP_INSTAGRAM: "url",
    PROP_TWITTER: "url",
    PROP_WEBSITE: "url",
    PROP_DEX_ID: "rich_text",
}

# LifeOS social_links platform -> Notion property.
PLATFORM_PROPERTY = {
    "linkedin": PROP_LINKEDIN,
    "instagram": PROP_INSTAGRAM,
    "twitter": PROP_TWITTER,
    "x": PROP_TWITTER,
    "website": PROP_WEBSITE,
    "web": PROP_WEBSITE,
    "personal": PROP_WEBSITE,
    "personal_website": PROP_WEBSITE,
    "site": PROP_WEBSITE,
    "homepage": PROP_WEBSITE,
}


def _platforms_for(prop_name: str) -> set:
    return {p for p, v in PLATFORM_PROPERTY.items() if v == prop_name}


# Fields --fill-only-fields may name: they then fill an empty Notion cell only.
FILL_ONLY_ALLOWED = (
    PROP_EMAIL, PROP_PHONE, PROP_COMPANY, PROP_TITLE, PROP_LINKEDIN, PROP_INSTAGRAM,
    PROP_TWITTER, PROP_WEBSITE, PROP_CHECKIN,
)

EXIT_OK = 0
EXIT_WRITE_FAILURES = 1
EXIT_CONFIG = 2
EXIT_MAPPING_ABORT = 3

logger = logging.getLogger("notion_people_sync")


# ---------------------------------------------------------------------------
# Errors and secret handling
# ---------------------------------------------------------------------------

class NotionError(RuntimeError):
    """A Notion API failure. The message never contains the token."""

    def __init__(self, message: str, status: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.code = code


class LifeOSUnavailable(RuntimeError):
    """The LifeOS API could not be read."""


class MapFileError(RuntimeError):
    """The stored mapping file exists but cannot be read."""


class ConfigError(RuntimeError):
    """An option or setting has an invalid value."""


class LedgerError(RuntimeError):
    """The ledger file cannot be appended to."""


class MappingAbort(RuntimeError):
    """Too few Notion rows matched a LifeOS person to trust the mapping."""


def redact(text: Any, *secrets: str) -> str:
    """Replace every non-empty secret in text with ***."""
    out = str(text)
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


class RedactingFilter(logging.Filter):
    """Scrubs secrets from every log record before it is emitted."""

    def __init__(self, *secrets: str):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            record.msg = redact(record.getMessage(), *self._secrets)
            record.args = ()
        return True


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def last10(value: Optional[str]) -> Optional[str]:
    """Last 10 digits of a phone number, or None when it has fewer than 10."""
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else None


def normalize_email(value: Optional[str]) -> str:
    return (value or "").strip().lower()


_LINKEDIN_COUNTRY_HOST = re.compile(r"^[a-z]{2,3}\.linkedin\.com")
_X_HOST = re.compile(r"^x\.com(?=/|$)")


def normalize_url(url: Optional[str]) -> Optional[str]:
    """Lowercase, no scheme, www., query, fragment or trailing slash. x.com is
    read as twitter.com. Returns None for an empty value."""
    if not url or not str(url).strip():
        return None
    s = unquote(str(url).strip()).lower()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = re.split(r"[?#]", s, maxsplit=1)[0].rstrip("/")
    s = _LINKEDIN_COUNTRY_HOST.sub("linkedin.com", s)
    s = _X_HOST.sub("twitter.com", s)
    return s or None


def normalize_linkedin(url: Optional[str]) -> Optional[str]:
    """normalize_url, or None unless the result contains linkedin.com/in/."""
    s = normalize_url(url)
    return s if s and "linkedin.com/in/" in s else None


def https_url(url: str) -> Optional[str]:
    """The URL with an https scheme and no query or fragment, or None when it
    has no host containing a dot (a bare handle such as "@annlong")."""
    s = re.split(r"[?#]", url.strip(), maxsplit=1)[0].rstrip("/")
    if s.lower().startswith("http://"):
        s = "https://" + s[7:]
    elif not re.match(r"^[a-z][a-z0-9+.-]*://", s, re.IGNORECASE):
        s = "https://" + s
    host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s, flags=re.IGNORECASE).split("/", 1)[0]
    if "." not in host or "@" in host or re.search(r"\s", host):
        return None
    return s


def norm_text(value: Optional[str]) -> str:
    """Lowercase, '&' read as 'and', punctuation and quotes removed, spaces
    collapsed. This is the form in which Company and Title values are compared."""
    s = (value or "").lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]|_", " ", s)
    return " ".join(s.split())


def reject_reason(value: str) -> Optional[str]:
    """Why a LifeOS Company or Title value must not be written, or None."""
    if not any(ch.isalpha() for ch in value):
        return "no letters"
    if EMAIL_RE.search(value):
        return "looks like an email address"
    if len(value) > MAX_VALUE_LEN:
        return f"longer than {MAX_VALUE_LEN} characters"
    if "|" in value:
        return "looks like a headline"
    return None


def notion_is_richer(new_norm: str, current_norm: str) -> bool:
    """True when the normalised LifeOS value is a proper substring, or its word
    set a proper subset, of the normalised Notion value."""
    if not new_norm or not current_norm or new_norm == current_norm:
        return False
    return new_norm in current_norm or set(new_norm.split()) < set(current_norm.split())


def name_key(name: Optional[str]) -> str:
    """Normalised full name (credentials after a comma dropped) for equality."""
    return " ".join(name_tokens(name))


def parse_fill_only(text: Optional[str]) -> frozenset:
    """Validate a comma-separated list of Notion property names. Raises
    ConfigError naming any unknown entry."""
    wanted = [t.strip() for t in (text or "").split(",") if t.strip()]
    lookup = {name.casefold(): name for name in FILL_ONLY_ALLOWED}
    unknown = [t for t in wanted if t.casefold() not in lookup]
    if unknown:
        raise ConfigError(
            f"Unknown field(s) in --fill-only-fields / LIFEOS_NOTION_FILL_ONLY_FIELDS: "
            f"{', '.join(unknown)}. Allowed: {', '.join(FILL_ONLY_ALLOWED)}"
        )
    return frozenset(lookup[t.casefold()] for t in wanted)


def name_tokens(name: Optional[str]) -> list[str]:
    """Case-folded, letters-only tokens of a name, cut at the first comma
    (which drops credentials such as ", CFP")."""
    tokens = []
    for raw in (name or "").split(",", 1)[0].split():
        token = "".join(ch for ch in raw if ch.isalpha()).casefold()
        if token:
            tokens.append(token)
    return tokens


_PAREN = re.compile(r"\(([^)]*)\)")


def name_candidates(name: Optional[str]) -> list[list[str]]:
    """Token lists a LifeOS name can be known by. Without parentheses that is
    just its own tokens. "Adrienne (Szarmack) Smith" gives ["adrienne", "smith"]
    (parenthesised part removed), ["adrienne", "szarmack"] (the parenthesised
    word replacing the surname) and ["szarmack", "smith"] (it replacing the
    first name, as in "Bob (Robert) Lee" -> "Robert Lee")."""
    cut = (name or "").split(",", 1)[0]
    plain = name_tokens(_PAREN.sub(" ", cut))
    candidates = [plain] if plain else []
    inner = _PAREN.search(cut)
    inner_tokens = name_tokens(inner.group(1)) if inner else []
    if plain and inner_tokens:
        candidates.append([plain[0], inner_tokens[-1]])
        if len(plain) > 1:
            candidates.append([inner_tokens[0], plain[-1]])
    return candidates


def names_compatible(notion_name: Optional[str], *lifeos_names: Optional[str]) -> bool:
    """True when the first AND last token of the Notion name equal the first and
    last token of at least one candidate of at least one LifeOS name."""
    theirs = name_tokens(notion_name)
    if not theirs:
        return False
    for name in lifeos_names:
        for ours in name_candidates(name):
            if ours[0] == theirs[0] and ours[-1] == theirs[-1]:
                return True
    return False


def _str_list(value: Any) -> list[str]:
    """A list of non-empty strings from a LifeOS list field."""
    out = []
    for item in value or []:
        if isinstance(item, dict):
            item = item.get("email") or item.get("phone") or item.get("value")
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _short(value: Any, width: int = 40) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def local_timezone_name() -> Optional[str]:
    """The machine's IANA time zone name, or None when it cannot be found."""
    env = os.environ.get("TZ", "").lstrip(":")
    if "/" in env:
        return env
    try:
        name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if "/" in name:
            return name
    except OSError:
        pass
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Notion rows
# ---------------------------------------------------------------------------

@dataclass
class NotionRow:
    page_id: str
    name: str = ""
    dex_id: str = ""
    lifeos_id: str = ""
    email: Optional[str] = None
    phone: Optional[str] = None
    company: str = ""
    title: str = ""
    linkedin: Optional[str] = None
    instagram: Optional[str] = None
    twitter: Optional[str] = None
    website: Optional[str] = None
    last_checkin: Optional[str] = None  # YYYY-MM-DD (or whatever Notion held)
    raw: dict = field(default_factory=dict, compare=False, repr=False)  # page properties as read


def _plain(rich: Any) -> str:
    return "".join(
        (part or {}).get("plain_text", "") for part in (rich or []) if isinstance(part, dict)
    ).strip()


def parse_row(page: dict) -> NotionRow:
    """Read one Notion page object into a NotionRow. Missing or empty
    properties become empty strings or None."""
    props = page.get("properties") or {}

    def prop(name: str) -> dict:
        value = props.get(name)
        return value if isinstance(value, dict) else {}

    start = (prop(PROP_CHECKIN).get("date") or {}).get("start")
    return NotionRow(
        page_id=str(page.get("id", "")),
        name=_plain(prop(PROP_NAME).get("title")),
        dex_id=_plain(prop(PROP_DEX_ID).get("rich_text")),
        lifeos_id=_plain(prop(PROP_LIFEOS_ID).get("rich_text")),
        email=(prop(PROP_EMAIL).get("email") or None),
        phone=(prop(PROP_PHONE).get("phone_number") or None),
        company=_plain(prop(PROP_COMPANY).get("rich_text")),
        title=_plain(prop(PROP_TITLE).get("rich_text")),
        linkedin=(prop(PROP_LINKEDIN).get("url") or None),
        instagram=(prop(PROP_INSTAGRAM).get("url") or None),
        twitter=(prop(PROP_TWITTER).get("url") or None),
        website=(prop(PROP_WEBSITE).get("url") or None),
        last_checkin=(str(start)[:10] if start else None),
        raw=props,
    )


# ---------------------------------------------------------------------------
# Mapping files and ledger
# ---------------------------------------------------------------------------

def load_json(path: Optional[Path]) -> dict:
    """A JSON object from path; {} when the file is absent or unreadable."""
    if not path or not Path(path).exists():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def load_stored_map(path: Optional[Path], strict: bool = False) -> dict[str, dict]:
    """The stored page mapping. An absent file is an empty mapping. A file that
    exists but cannot be parsed raises MapFileError when strict, else warns."""
    if not path or not Path(path).exists():
        return {}
    problem = None
    pages: Any = None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        pages = data.get("pages") if isinstance(data, dict) else None
        if not isinstance(pages, dict):
            problem = "not a JSON object with a 'pages' object"
    except (OSError, ValueError) as exc:
        problem = str(exc)
    if problem:
        message = f"Stored mapping {path} is unreadable ({problem})"
        if strict:
            raise MapFileError(message + "; fix or move it, nothing was written")
        logger.warning(message)
        return {}
    return pages


def load_dexmap(path: Optional[Path]) -> dict[str, str]:
    mapping = load_json(path).get("dex_to_lifeos")
    return mapping if isinstance(mapping, dict) else {}


def save_map_atomic(path: Path, pages: dict[str, dict]) -> None:
    """Write the stored mapping through a temp file and an atomic rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "pages": pages}, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Ledger:
    """Append-only JSON-lines record of every change an execute run makes.

    An "intent" line (with the old value) is flushed to disk before each
    request and a "result" line follows the response, so a request is never
    sent without its record."""

    def __init__(self, path: Path, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8"):
                pass
        except OSError as exc:
            raise LedgerError(f"Ledger {self.path} cannot be written: {exc}") from exc

    def record(self, phase: str, action: str, page_id: str, lifeos_id: str, field_name: str,
               old: Any, new: Any, old_full: Any = None, status: Optional[str] = None,
               error: Optional[str] = None) -> None:
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "phase": phase,
            "action": action,
            "page_id": page_id,
            "lifeos_id": lifeos_id,
            "field": field_name,
            "old": old,
            "new": new,
            "old_full": old_full,
        }
        if status:
            line["status"] = status
        if error:
            line["error"] = error
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise LedgerError(f"Ledger {self.path} cannot be written: {exc}") from exc


# ---------------------------------------------------------------------------
# Resolving Notion rows to LifeOS people
# ---------------------------------------------------------------------------

@dataclass
class Resolution:
    person: Optional[dict] = None
    matched_by: Optional[str] = None  # lifeos_id | dex_map | identifiers
    source: Optional[str] = None      # lifeos_id | map | dexmap | identifiers
    reason: Optional[str] = None      # why unmatched
    orphan: bool = False              # LifeOS_ID points at no person


@dataclass
class PeopleIndex:
    by_id: dict[str, dict] = field(default_factory=dict)
    by_email: dict[str, set] = field(default_factory=dict)
    by_phone: dict[str, set] = field(default_factory=dict)
    by_linkedin: dict[str, set] = field(default_factory=dict)


def _linkedin_of(person: dict) -> Optional[str]:
    return _clean(person.get("linkedin_url")) or social_url(person, {"linkedin"})


def build_index(people: list[dict]) -> PeopleIndex:
    idx = PeopleIndex()
    for person in people:
        pid = str(person.get("id") or "")
        if not pid:
            continue
        idx.by_id[pid] = person
        for email in _str_list(person.get("emails")):
            idx.by_email.setdefault(normalize_email(email), set()).add(pid)
        for phone in _str_list(person.get("phone_numbers")):
            key = last10(phone)
            if key:
                idx.by_phone.setdefault(key, set()).add(pid)
        link = normalize_linkedin(_linkedin_of(person))
        if link:
            idx.by_linkedin.setdefault(link, set()).add(pid)
    return idx


def _name_ok(row: NotionRow, person: dict) -> bool:
    return names_compatible(row.name, person.get("display_name"), person.get("canonical_name"))


def resolve_row(
    row: NotionRow,
    index: PeopleIndex,
    stored_map: dict[str, dict],
    dexmap: dict[str, str],
) -> Resolution:
    """Resolve one row: LifeOS_ID, stored map, Dex_ID seed, then identifiers."""
    if row.lifeos_id:
        if row.lifeos_id in index.by_id:
            return Resolution(person=index.by_id[row.lifeos_id], matched_by="lifeos_id",
                              source="lifeos_id")
        return Resolution(reason=f"orphan: LifeOS_ID {row.lifeos_id} matches no LifeOS person",
                          orphan=True)

    candidate: Optional[dict] = None
    matched_by = source = None

    stored = stored_map.get(row.page_id)
    if isinstance(stored, dict) and stored.get("lifeos_id") in index.by_id:
        seed_target = dexmap.get(row.dex_id) if row.dex_id else None
        if seed_target and seed_target != stored["lifeos_id"]:
            logger.warning(
                "Stored mapping for %s (%s) points at LifeOS %s but the seed map sends its "
                "Dex_ID to %s; skipping the row",
                row.name, row.page_id, stored["lifeos_id"], seed_target,
            )
            return Resolution(
                reason=f"stored mapping ({stored['lifeos_id']}) disagrees with seed map ({seed_target})"
            )
        candidate = index.by_id[stored["lifeos_id"]]
        matched_by = stored.get("matched_by") or "dex_map"
        source = "map"

    if candidate is None and row.dex_id and dexmap.get(row.dex_id) in index.by_id:
        candidate = index.by_id[dexmap[row.dex_id]]
        matched_by, source = "dex_map", "dexmap"

    if candidate is None:
        found: set[str] = set()
        tried = []
        if row.email:
            tried.append("email")
            found |= index.by_email.get(normalize_email(row.email), set())
        key = last10(row.phone)
        if key:
            tried.append("phone")
            found |= index.by_phone.get(key, set())
        link = normalize_linkedin(row.linkedin)
        if link:
            tried.append("linkedin")
            found |= index.by_linkedin.get(link, set())
        if not tried:
            return Resolution(reason="no mapping and no email, phone or LinkedIn to match on")
        if not found:
            return Resolution(reason=f"no LifeOS person matches {'/'.join(tried)}")
        if len(found) > 1:
            return Resolution(reason=f"ambiguous: {len(found)} LifeOS people match {'/'.join(tried)}")
        candidate = index.by_id[next(iter(found))]
        matched_by, source = "identifiers", "identifiers"

    if not _name_ok(row, candidate):
        return Resolution(
            reason=f"name mismatch ('{row.name}' vs '{candidate.get('display_name')}')"
        )
    return Resolution(person=candidate, matched_by=matched_by, source=source)


# ---------------------------------------------------------------------------
# Change rules
# ---------------------------------------------------------------------------

def rich_text(value: str) -> dict:
    if len(value) > MAX_TEXT:
        logger.warning("Text of %d characters truncated to %d for Notion", len(value), MAX_TEXT)
        value = value[:MAX_TEXT]
    return {"rich_text": [{"text": {"content": value}}]}


@dataclass
class FieldChange:
    field: str
    old: Any
    new: Any
    payload: dict  # Notion property value to write
    old_full: Any = None      # full Notion value replaced (rich_text array, date object)
    displaces: bool = False   # replaces an Email or Phone LifeOS has never seen


@dataclass
class Change:
    page_id: str
    name: str
    lifeos_id: str
    fields: list[FieldChange] = field(default_factory=list)
    id_value: Optional[str] = None  # LifeOS_ID to write, when the cell is empty

    def properties(self, with_fields: bool = True, with_id: bool = True) -> dict:
        props: dict = {}
        if with_fields:
            props.update({f.field: f.payload for f in self.fields})
        if with_id and self.id_value:
            props[PROP_LIFEOS_ID] = rich_text(self.id_value)
        return props


def social_url(person: dict, platforms: set) -> Optional[str]:
    """First social_links URL whose platform is in platforms."""
    for item in person.get("social_links") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("platform") or "").strip().lower() in platforms:
            url = _clean(item.get("url")) or _clean(item.get("value"))
            if url:
                return url
    return None


def last_seen_local_date(
    last_seen: Optional[str], now: Optional[datetime] = None, tz: Optional[tzinfo] = None
) -> Optional[date]:
    """Local calendar date of an ISO timestamp, or None when it is missing,
    unparseable or in the future. A timestamp without an offset is read as UTC.
    tz=None means the machine's local zone."""
    if not last_seen:
        return None
    try:
        dt = datetime.fromisoformat(str(last_seen).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if dt > now:
        return None
    return dt.astimezone(tz).date()


def _old_full(row: NotionRow, prop_name: str, plain: Any) -> Any:
    """The complete Notion value of a property: the rich_text array or date
    object as read, else the plain value."""
    raw = row.raw.get(prop_name) if isinstance(row.raw, dict) else None
    if isinstance(raw, dict):
        if isinstance(raw.get("rich_text"), list):
            return raw["rich_text"] or None
        if "date" in raw:
            return raw["date"]
    return plain


def compute_changes(
    row: NotionRow,
    person: dict,
    now: Optional[datetime] = None,
    tz: Optional[tzinfo] = None,
    available: Optional[set] = None,
    fill_only: frozenset = frozenset(),
    skips: Optional[list] = None,
) -> tuple[Optional[Change], list[str]]:
    """What to write for one matched row, plus messages for values that cannot
    be compared. LifeOS wins; an empty LifeOS value never blanks Notion.
    available is the set of optional properties the schema has (None = all);
    fill_only names fields that may only fill an empty Notion cell; skips
    receives a message for each LifeOS value that was not written on quality
    grounds."""
    fields: list[FieldChange] = []
    conflicts: list[str] = []

    def note(message: str) -> None:
        if skips is not None:
            skips.append(message)

    def fill_blocked(prop_name: str, current: Any) -> bool:
        return prop_name in fill_only and bool(current)

    emails = _str_list(person.get("emails"))
    if emails and (not row.email or normalize_email(row.email) not in {normalize_email(e) for e in emails}):
        if not fill_blocked(PROP_EMAIL, row.email):
            fields.append(FieldChange(PROP_EMAIL, row.email, emails[0], {"email": emails[0]},
                                      _old_full(row, PROP_EMAIL, row.email), displaces=bool(row.email)))

    phones = _str_list(person.get("phone_numbers"))
    known_phones = {last10(p) for p in phones if last10(p)}
    if phones and (not row.phone or last10(row.phone) not in known_phones):
        if not fill_blocked(PROP_PHONE, row.phone):
            fields.append(FieldChange(PROP_PHONE, row.phone, phones[0], {"phone_number": phones[0]},
                                      _old_full(row, PROP_PHONE, row.phone), displaces=bool(row.phone)))

    for prop_name, value, current in (
        (PROP_COMPANY, _clean(person.get("company")), row.company),
        (PROP_TITLE, _clean(person.get("position")), row.title),
    ):
        if not value or fill_blocked(prop_name, current.strip()):
            continue
        reason = reject_reason(value)
        if reason:
            note(f"{prop_name}: LifeOS value not written ({reason}): {_short(value)}")
            continue
        new_norm, current_norm = norm_text(value), norm_text(current)
        if new_norm == current_norm:
            continue
        if notion_is_richer(new_norm, current_norm):
            note(f"{prop_name}: Notion value is richer, kept: {_short(current)} (LifeOS: {_short(value)})")
            continue
        fields.append(FieldChange(prop_name, current or None, value, rich_text(value),
                                  _old_full(row, prop_name, current or None)))

    wanted_urls = {
        PROP_LINKEDIN: (_linkedin_of(person), row.linkedin),
        PROP_INSTAGRAM: (social_url(person, _platforms_for(PROP_INSTAGRAM)), row.instagram),
        PROP_TWITTER: (social_url(person, _platforms_for(PROP_TWITTER)), row.twitter),
        PROP_WEBSITE: (social_url(person, _platforms_for(PROP_WEBSITE)), row.website),
    }
    for prop_name, (value, current) in wanted_urls.items():
        if not value or (available is not None and prop_name not in available):
            continue
        if fill_blocked(prop_name, current):
            continue
        new_norm = normalize_url(value)
        if new_norm and new_norm != normalize_url(current):
            url = https_url(value)
            if url is None:
                note(f"{prop_name}: LifeOS value is not a URL, not written: {_short(value)}")
                continue
            fields.append(FieldChange(prop_name, current, url, {"url": url}, current))

    seen = last_seen_local_date(person.get("last_seen"), now=now, tz=tz)
    if seen is not None and not fill_blocked(PROP_CHECKIN, row.last_checkin):
        current_date: Optional[date] = None
        readable = True
        if row.last_checkin:
            try:
                current_date = date.fromisoformat(row.last_checkin[:10])
            except ValueError:
                readable = False
                conflicts.append(f"Last Check-In: unreadable Notion date '{row.last_checkin}'")
        if readable and (current_date is None or current_date < seen):
            fields.append(FieldChange(PROP_CHECKIN, row.last_checkin, seen.isoformat(),
                                      {"date": {"start": seen.isoformat()}},
                                      _old_full(row, PROP_CHECKIN, row.last_checkin)))

    id_value = str(person.get("id")) if not row.lifeos_id and row.page_id else None
    if not fields and not id_value:
        return None, conflicts
    return Change(row.page_id, row.name, str(person.get("id")), fields, id_value), conflicts


@dataclass
class Applied:
    change: Change
    with_fields: bool
    with_id: bool


def apply_caps(
    changes: list[Change], max_writes: int, max_id_writes: int
) -> tuple[list[Applied], int, int]:
    """Order changes by name and apply the two caps. Returns the pages to
    write, the number of field updates deferred and id writes deferred. A page
    over one cap is still written for the other kind of change."""
    ordered = sorted(changes, key=lambda c: (c.name.casefold(), c.page_id))
    updates = ids = deferred_updates = deferred_ids = 0
    applied: list[Applied] = []
    for change in ordered:
        do_fields = bool(change.fields) and updates < max(0, max_writes)
        do_id = bool(change.id_value) and ids < max(0, max_id_writes)
        if change.fields and not do_fields:
            deferred_updates += 1
        if change.id_value and not do_id:
            deferred_ids += 1
        if do_fields:
            updates += 1
        if do_id:
            ids += 1
        if do_fields or do_id:
            applied.append(Applied(change, do_fields, do_id))
    return applied, deferred_updates, deferred_ids


# ---------------------------------------------------------------------------
# New pages
# ---------------------------------------------------------------------------

def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def qualifies_for_page(person: dict) -> bool:
    """A person worth a Notion page: not the user themself, with a usable name
    (not empty, no "@"), and with real interaction (a message, email or
    meeting) or a place in the closest circles. A bare last_seen is not enough."""
    if str(person.get("category") or "").lower() == "self":
        return False
    name = _clean(person.get("display_name"))
    if not name or "@" in name:
        return False
    emails = _str_list(person.get("emails"))
    if emails and all(is_bulk_sender_email(e) for e in emails):
        return False  # a vendor or newsletter sender, not a person
    return bool(
        _int(person.get("message_count")) > 0
        or _int(person.get("email_count")) > 0
        or _int(person.get("meeting_count")) > 0
        or 1 <= _int(person.get("dunbar_circle")) <= 5
    )


def _strength(person: dict) -> float:
    try:
        return float(person.get("relationship_strength") or 0)
    except (TypeError, ValueError):
        return 0.0


def person_name_keys(person: dict) -> set:
    keys = {" ".join(c) for c in name_candidates(person.get("display_name"))}
    keys.add(name_key(person.get("canonical_name")))
    keys.discard("")
    return keys


_TITLES_RE = re.compile(r"^(?:(?:dr|mr|mrs|ms|miss|prof)\.?\s+)+", re.IGNORECASE)
_CREDENTIALS = {"phd", "md", "esq", "jr", "sr", "ii", "iii", "iv", "mba", "cpa", "cfp", "jd", "rn",
                "dds", "pe", "chfc", "clu", "cfa"}


def _is_credentials(text: str) -> bool:
    tokens = ["".join(ch for ch in t if ch.isalpha()) for t in text.split()]
    tokens = [t for t in tokens if t]
    return bool(tokens) and all(t.lower() in _CREDENTIALS or (t.isupper() and len(t) <= 5) for t in tokens)


def loose_name_keys(name: Optional[str]) -> set:
    """Keys "surname|first initial" for a name, after handling "Last, First"
    order and dropping titles (Dr, Mr, ...) and credentials after a comma. A
    parenthesised part is handled like name_candidates. Single-word names give
    no key."""
    text = _TITLES_RE.sub("", (name or "").strip())
    if "," in text:
        before, after = text.split(",", 1)
        if len(before.split()) == 1 and after.split() and not _is_credentials(after):
            text = f"{after.strip()} {before.strip()}"
        else:
            text = before
    text = _TITLES_RE.sub("", text.strip())
    keys = set()
    for cand in name_candidates(text):
        if len(cand) >= 2:
            keys.add(f"{cand[-1]}|{cand[0][0]}")
    return keys


def person_loose_keys(person: dict) -> set:
    return loose_name_keys(person.get("display_name")) | loose_name_keys(person.get("canonical_name"))


_BULK_LOCAL_PARTS = {"noreply", "no-reply", "donotreply", "do-not-reply", "notifications",
                     "notification", "newsletter", "news", "marketing", "mailer", "bounce"}
_BULK_FIRST_LABEL = re.compile(r"^(em\d+|(mail|email|mg|bounce|notify)\d*)$")


def is_bulk_sender_email(address: str) -> bool:
    """True for an address that looks like an automated sender. The domain rule
    needs a subdomain (three or more labels), so a person on mail.com is not
    caught."""
    local, _, domain = address.strip().lower().rpartition("@")
    if not domain:
        return False
    if local in _BULK_LOCAL_PARTS:
        return True
    labels = domain.split(".")
    return len(labels) >= 3 and bool(_BULK_FIRST_LABEL.match(labels[0]))


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    rows: int = 0
    matched: int = 0
    primary_matched: int = 0   # kept equal to matched; the abort ratio uses it
    unmatched: list[tuple[NotionRow, str]] = field(default_factory=list)
    orphans: list[NotionRow] = field(default_factory=list)
    duplicates: list[NotionRow] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    conflicts: list[tuple[NotionRow, str]] = field(default_factory=list)
    skips: list[tuple[NotionRow, str]] = field(default_factory=list)
    possible_matches: list[tuple[NotionRow, dict]] = field(default_factory=list)
    duplicate_name_skips: list[tuple[dict, str]] = field(default_factory=list)
    possible_duplicate_skips: list[tuple[dict, str]] = field(default_factory=list)
    new_mappings: dict[str, dict] = field(default_factory=dict)
    creates: list[dict] = field(default_factory=list)  # people, closest first


def build_plan(
    rows: list[NotionRow],
    people: list[dict],
    stored_map: dict[str, dict],
    dexmap: dict[str, str],
    now: Optional[datetime] = None,
    tz: Optional[tzinfo] = None,
    today: Optional[date] = None,
    available: Optional[set] = None,
    fill_only: frozenset = frozenset(),
) -> Plan:
    index = build_index(people)
    plan = Plan(rows=len(rows))
    stamp = (today or date.today()).isoformat()
    claimed: dict[str, str] = {}  # person id -> page id

    counts: dict[str, int] = {}
    for row in rows:
        if row.lifeos_id:
            counts[row.lifeos_id] = counts.get(row.lifeos_id, 0) + 1
    duplicate_ids = {i for i, n in counts.items() if n > 1}

    def accept(row: NotionRow, res: Resolution) -> None:
        plan.matched += 1
        claimed[str(res.person["id"])] = row.page_id
        if res.source != "map":
            plan.new_mappings[row.page_id] = {
                "lifeos_id": str(res.person["id"]),
                "dex_id": row.dex_id,
                "matched_by": res.matched_by,
                "added": stamp,
            }
        skips: list[str] = []
        change, conflicts = compute_changes(row, res.person, now=now, tz=tz, available=available,
                                            fill_only=fill_only, skips=skips)
        if change:
            plan.changes.append(change)
        plan.conflicts.extend((row, c) for c in conflicts)
        plan.skips.extend((row, m) for m in skips)

    ordered = sorted(rows, key=lambda r: (r.name.casefold(), r.page_id))
    with_id = [r for r in ordered if r.lifeos_id]
    without_id = [r for r in ordered if not r.lifeos_id]

    for row in with_id:
        if row.lifeos_id in duplicate_ids:
            plan.duplicates.append(row)
            plan.unmatched.append((row, f"duplicate: LifeOS_ID {row.lifeos_id} is on several rows"))
            continue
        res = resolve_row(row, index, stored_map, dexmap)
        if res.person is None:
            plan.orphans.append(row)
            plan.unmatched.append((row, res.reason or "orphan"))
            continue
        accept(row, res)

    for row in without_id:
        res = resolve_row(row, index, stored_map, dexmap)
        if res.person is None:
            plan.unmatched.append((row, res.reason or "unmatched"))
            continue
        pid = str(res.person["id"])
        if pid in claimed:
            plan.unmatched.append((row, f"LifeOS person {pid} is already linked to another row"))
            continue
        accept(row, res)

    plan.primary_matched = plan.matched

    # Pages to create. A person is never created when a row already carries
    # their id, the stored mapping holds them (including a create whose outcome
    # is unknown), or any Notion row has their name.
    stored_ids = {v.get("lifeos_id") for v in stored_map.values() if isinstance(v, dict)}
    row_ids = {r.lifeos_id for r in rows if r.lifeos_id}
    candidates = [
        p for p in people
        if str(p.get("id")) not in claimed and str(p.get("id")) not in stored_ids
        and str(p.get("id")) not in row_ids and qualifies_for_page(p)
    ]

    # A row that fits one candidate only by name is reported, never written.
    by_person: dict[str, list[NotionRow]] = {}
    by_row: dict[str, list[dict]] = {}
    for row, reason in plan.unmatched:
        if not reason.startswith("no "):
            continue
        for person in candidates:
            if names_compatible(row.name, person.get("display_name"), person.get("canonical_name")):
                by_row.setdefault(row.page_id, []).append(person)
                by_person.setdefault(str(person["id"]), []).append(row)
    possible_ids: set[str] = set()
    flagged: dict[str, str] = {}
    for row, _reason in plan.unmatched:
        persons = by_row.get(row.page_id, [])
        if len(persons) != 1 or len(by_person[str(persons[0]["id"])]) != 1:
            continue
        person = persons[0]
        plan.possible_matches.append((row, person))
        possible_ids.add(str(person["id"]))
        flagged[row.page_id] = (f"possible match, review by hand: {person.get('display_name')} "
                                f"({person.get('id')})")
    plan.unmatched = [(r, flagged.get(r.page_id, why)) for r, why in plan.unmatched]

    row_keys = {name_key(r.name) for r in rows} - {""}
    row_loose: set[str] = set()
    for r in rows:
        row_loose |= loose_name_keys(r.name)
    planned_keys: set[str] = set()
    planned_loose: set[str] = set()
    for person in sorted(
        candidates,
        key=lambda p: (-_strength(p), str(p.get("display_name") or "").casefold(), str(p["id"])),
    ):
        pid = str(person["id"])
        if pid in possible_ids:
            continue
        keys = person_name_keys(person)
        if keys & row_keys:
            plan.duplicate_name_skips.append((person, "a Notion row already has this name"))
            continue
        if keys & planned_keys:
            plan.duplicate_name_skips.append((person, "another person planned in this run has this name"))
            continue
        loose = person_loose_keys(person)
        if loose & row_loose:
            plan.possible_duplicate_skips.append(
                (person, "possible duplicate, review by hand: a Notion row has the same surname and first initial"))
            continue
        if loose & planned_loose:
            plan.possible_duplicate_skips.append(
                (person, "possible duplicate, review by hand: another person planned in this run has the "
                         "same surname and first initial"))
            continue
        planned_keys |= keys
        planned_loose |= loose
        plan.creates.append(person)
    return plan


def check_mapping_ratio(plan: Plan) -> None:
    """Raise MappingAbort when under half of the Notion rows matched."""
    if plan.rows and plan.primary_matched / plan.rows < MIN_MATCH_RATIO:
        raise MappingAbort(
            f"only {plan.primary_matched} of {plan.rows} Notion rows matched a LifeOS person "
            f"(minimum {int(MIN_MATCH_RATIO * 100)}%); the mapping looks broken, nothing written"
        )


# ---------------------------------------------------------------------------
# Notion client
# ---------------------------------------------------------------------------

class NotionClient:
    """Thin Notion REST client: paced, retrying, token-redacting."""

    def __init__(
        self,
        token: str,
        data_source_id: str,
        *,
        version: str = DEFAULT_NOTION_VERSION,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
        pace: float = PACE_SECONDS,
        max_retries: int = MAX_RETRIES,
        base_url: str = NOTION_API,
    ):
        self._token = token
        self.data_source_id = data_source_id
        self._sleep = sleep
        self._pace = pace
        self._max_retries = max_retries
        self._last_request: Optional[float] = None
        self._http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": version,
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._http.close()

    def _fail(self, message: str, status: Optional[int] = None, code: Optional[str] = None) -> NotionError:
        return NotionError(redact(message, self._token), status, code)

    @staticmethod
    def _detail(resp: httpx.Response) -> tuple[Optional[str], str]:
        """The Notion error code and message from an error response."""
        try:
            body = resp.json()
        except ValueError:
            return None, resp.text[:300]
        if isinstance(body, dict):
            return body.get("code"), str(body.get("message") or "")[:500]
        return None, str(body)[:300]

    def _request(self, method: str, path: str, payload: Optional[dict] = None,
                 retry_server_errors: bool = True) -> dict:
        """One paced request. retry_server_errors=False limits retries to 409
        and 429, for calls that must not run twice (page creation)."""
        for attempt in range(self._max_retries + 1):
            if self._last_request is not None:
                wait = self._pace - (time.monotonic() - self._last_request)
                if wait > 0:
                    self._sleep(wait)
            self._last_request = time.monotonic()
            try:
                resp = (
                    self._http.request(method, path, json=payload)
                    if payload is not None
                    else self._http.request(method, path)
                )
            except httpx.TransportError as exc:
                if attempt >= self._max_retries or not retry_server_errors:
                    raise self._fail(f"Notion request failed: {type(exc).__name__}: {exc}")
                self._sleep(min(2.0 ** attempt, 30.0))
                continue

            status = resp.status_code
            if status < 300:
                try:
                    return resp.json()
                except ValueError:
                    raise self._fail(f"Notion returned HTTP {status} with a body that is not JSON", status)
            code, message = self._detail(resp)
            detail = f" [{code}: {message}]" if (code or message) else ""
            retryable = status in (409, 429) or (status >= 500 and retry_server_errors)
            if retryable:
                if attempt >= self._max_retries:
                    raise self._fail(f"Notion returned HTTP {status} after {attempt + 1} attempts{detail}",
                                     status, code)
                delay = min(2.0 ** attempt, 30.0)
                if status == 429:
                    try:
                        delay = float(resp.headers.get("Retry-After", delay))
                    except ValueError:
                        pass
                self._sleep(min(max(delay, 0.0), MAX_RETRY_WAIT))
                continue
            if status == 401:
                raise self._fail(f"Notion rejected the token (HTTP 401): check LIFEOS_NOTION_TOKEN{detail}",
                                 status, code)
            if status in (403, 404):
                raise self._fail(
                    f"Notion returned HTTP {status}: the data source or page is not shared with the "
                    "integration (Connections menu), the integration lacks a capability, or the ID "
                    f"is wrong{detail}",
                    status, code,
                )
            raise self._fail(f"Notion returned HTTP {status}{detail}", status, code)
        raise self._fail("Notion request failed")  # pragma: no cover

    def check_schema(self) -> set:
        """Verify the data source has the properties the sync needs, with the
        expected types. Returns the names of the optional properties present."""
        data = self._request("GET", f"/data_sources/{self.data_source_id}")
        found = data.get("properties")
        found = found if isinstance(found, dict) else {}
        for name, expected in REQUIRED_PROPERTIES.items():
            if name not in found:
                raise self._fail(
                    f"Notion data source has no property '{name}' (expected type {expected}); "
                    f"properties found: {', '.join(sorted(found)) or 'none'}"
                )
            actual = (found[name] or {}).get("type")
            if actual != expected:
                raise self._fail(f"Notion property '{name}' has type {actual}, expected {expected}")
        present = set()
        for name, expected in OPTIONAL_PROPERTIES.items():
            if name not in found:
                continue
            actual = (found[name] or {}).get("type")
            if actual != expected:
                raise self._fail(f"Notion property '{name}' has type {actual}, expected {expected}")
            present.add(name)
        return present

    def query_rows(self, limit: Optional[int] = None) -> list[dict]:
        """All pages of the People data source (following next_cursor)."""
        pages: list[dict] = []
        cursor: Optional[str] = None
        while True:
            payload: dict = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data = self._request("POST", f"/data_sources/{self.data_source_id}/query", payload)
            pages.extend(data.get("results") or [])
            if limit and len(pages) >= limit:
                return pages[:limit]
            if not data.get("has_more") or not data.get("next_cursor"):
                return pages
            cursor = data["next_cursor"]

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self._request("PATCH", f"/pages/{page_id}", {"properties": properties})

    def create_page(self, name: str, lifeos_id: str, template_id: str = "",
                    timezone_name: Optional[str] = None) -> dict:
        """Create a page holding only Full Name and LifeOS_ID, from the template
        when one is given. Never retried after a server error."""
        payload: dict = {
            "parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
            "properties": {
                PROP_NAME: {"title": [{"text": {"content": name}}]},
                PROP_LIFEOS_ID: rich_text(lifeos_id),
            },
        }
        if template_id:
            template: dict = {"type": "template_id", "template_id": template_id}
            if timezone_name:
                template["timezone"] = timezone_name
            payload["template"] = template
        return self._request("POST", "/pages", payload, retry_server_errors=False)

    def wait_for_template(self, page_id: str, interval: float = TEMPLATE_POLL_INTERVAL,
                          timeout: float = TEMPLATE_POLL_TIMEOUT) -> bool:
        """Poll the page's block children until the template content exists.
        Returns False when it has not appeared within timeout seconds."""
        waited = 0.0
        while True:
            try:
                data = self._request("GET", f"/blocks/{page_id}/children?page_size=1")
            except NotionError as exc:
                if exc.status == 401:
                    raise
                logger.warning("Could not read the new page's content: %s", exc)
                return False
            if data.get("results"):
                return True
            if waited >= timeout:
                return False
            self._sleep(interval)
            waited += interval


# ---------------------------------------------------------------------------
# LifeOS API
# ---------------------------------------------------------------------------

def fetch_people(api_base: str, transport: Optional[httpx.BaseTransport] = None) -> list[dict]:
    url = api_base.rstrip("/") + "/api/crm/people"
    try:
        with httpx.Client(transport=transport, timeout=120.0) as client:
            resp = client.get(url, params={"limit": 5000})
            resp.raise_for_status()
            body = resp.json()
            people = body.get("people") if isinstance(body, dict) else None
    except httpx.HTTPError as exc:
        raise LifeOSUnavailable(
            f"Could not read people from the LifeOS API at {api_base} "
            f"({type(exc).__name__}). Is the API running?"
        ) from exc
    except ValueError as exc:
        raise LifeOSUnavailable(f"LifeOS API at {api_base} returned invalid JSON") from exc
    if not isinstance(people, list):
        raise LifeOSUnavailable(f"LifeOS API at {api_base} returned no 'people' list")
    total = body.get("total")
    if body.get("has_more") or (isinstance(total, int) and total > len(people)):
        raise LifeOSUnavailable(
            f"LifeOS API at {api_base} returned a truncated people list "
            f"({len(people)} of {total if isinstance(total, int) else 'more'}); refusing to sync"
        )
    return people


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def new_stats(execute: bool, max_writes: int, max_id_writes: int = DEFAULT_MAX_ID_WRITES,
              max_creates: int = DEFAULT_MAX_CREATES) -> dict:
    return {
        "mode": "execute" if execute else "dry-run",
        "status": "ok",
        "exit_code": EXIT_OK,
        "rows": 0,
        "matched": 0,
        "unmatched": 0,
        "orphans": 0,
        "duplicates": 0,
        "possible_matches": 0,
        "changes": 0,
        "writes": 0,
        "id_writes": 0,
        "creates": 0,
        "template_pending": 0,
        "pending_creates": 0,
        "failed": 0,
        "conflicts": 0,
        "displaced_values": 0,
        "skipped_values": 0,
        "duplicate_name_skips": 0,
        "possible_duplicate_skips": 0,
        "deferred": 0,
        "planned_id_writes": 0,
        "deferred_id_writes": 0,
        "planned_creates": 0,
        "deferred_creates": 0,
        "creates_stopped": None,
        "updates_stopped": None,
        "new_mappings": 0,
        "fill_only": [],
        "max_writes": max_writes,
        "max_id_writes": max_id_writes,
        "max_creates": max_creates,
        "ledger_path": None,
    }


def _log_change(verb: str, item: Applied) -> None:
    change = item.change
    if item.with_id:
        logger.info("%s %s (%s): LifeOS_ID -> %s", verb, change.name, change.page_id, change.id_value)
    if item.with_fields:
        for f in change.fields:
            logger.info("%s %s (%s): %s: %s -> %s", verb, change.name, change.page_id,
                        f.field, _short(f.old) or "(empty)", _short(f.new))


def _patch_with_ledger(client: Any, ledger: Ledger, page_id: str, lifeos_id: str, action: str,
                       entries: list, properties: dict) -> None:
    """One PATCH, preceded by durable intent lines and followed by result lines.
    A ledger failure before the request stops the request."""
    for field_name, old, new, old_full in entries:
        ledger.record("intent", action, page_id, lifeos_id, field_name, old, new, old_full)
    try:
        client.update_page(page_id, properties)
    except NotionError as exc:
        for field_name, old, new, old_full in entries:
            ledger.record("result", action, page_id, lifeos_id, field_name, old, new, old_full,
                          status="failed", error=str(exc))
        raise
    for field_name, old, new, old_full in entries:
        ledger.record("result", action, page_id, lifeos_id, field_name, old, new, old_full, status="ok")


def _create_page_flow(client: Any, person: dict, template_id: str, timezone_name: Optional[str],
                      now: Optional[datetime], tz: Optional[tzinfo], available: Optional[set],
                      ledger: Ledger, stored_map: dict, map_path: Path,
                      stamp: str, stats: dict, fill_only: frozenset) -> None:
    """Create one page, wait for its template, then write the other properties.

    A pending entry is stored before the request. It stays when the outcome is
    unknown (timeout, transport error, 5xx) so the person is never created
    twice, and is replaced by the page entry when the page is created."""
    name = _clean(person.get("display_name")) or _clean(person.get("canonical_name")) or str(person["id"])
    pid = str(person["id"])
    pending_key = f"pending:{pid}"
    ledger.record("intent", "create", "", pid, "page", None, name)
    stored_map[pending_key] = {"lifeos_id": pid, "dex_id": "", "matched_by": "pending_create",
                               "added": stamp, "pending": True, "runs": 1}
    save_map_atomic(Path(map_path), stored_map)
    try:
        page = client.create_page(name, pid, template_id, timezone_name)
    except NotionError as exc:
        unknown = exc.status is None or exc.status >= 500
        ledger.record("result", "create", "", pid, "page", None, name,
                      status="unknown" if unknown else "failed", error=str(exc))
        if unknown:
            stats["pending_creates"] += 1
            logger.warning("Outcome of creating %s (LifeOS %s) is unknown; it stays pending and "
                           "is not created again until a row with that LifeOS_ID appears", name, pid)
        else:
            stored_map.pop(pending_key, None)
            save_map_atomic(Path(map_path), stored_map)
        raise
    page_id = str(page.get("id") or "")
    stats["creates"] += 1
    logger.info("CREATED %s (%s)", name, page_id)
    ledger.record("result", "create", page_id, pid, "page", None, name, status="ok")
    stored_map.pop(pending_key, None)
    stored_map[page_id] = {"lifeos_id": pid, "dex_id": "", "matched_by": "created", "added": stamp}
    save_map_atomic(Path(map_path), stored_map)

    if template_id and not client.wait_for_template(page_id):
        stats["template_pending"] += 1
        logger.warning("Template content for %s (%s) has not appeared; writing properties anyway",
                       name, page_id)

    row = NotionRow(page_id=page_id, name=name, lifeos_id=pid)
    skips: list[str] = []
    change, _ = compute_changes(row, person, now=now, tz=tz, available=available,
                                fill_only=fill_only, skips=skips)
    for message in skips:
        logger.info("SKIPPED %s (%s): %s", name, page_id, message)
    if not change or not change.fields:
        return
    entries = [(f.field, None, f.new, None) for f in change.fields]
    try:
        _patch_with_ledger(client, ledger, page_id, pid, "create", entries,
                           change.properties(with_id=False))
    except NotionError as exc:
        if exc.status == 401:
            raise
        stats["failed"] += 1
        logger.error("FAILED properties of new page %s (%s): %s", name, page_id, exc)
        return
    for f in change.fields:
        logger.info("CREATED %s (%s): %s -> %s", name, page_id, f.field, _short(f.new))


def run_sync(
    client: Any,
    people: list[dict],
    *,
    execute: bool = False,
    max_writes: int = DEFAULT_MAX_WRITES,
    max_id_writes: int = DEFAULT_MAX_ID_WRITES,
    max_creates: int = DEFAULT_MAX_CREATES,
    map_path: Optional[Path] = DEFAULT_MAP_PATH,
    dexmap_path: Optional[Path] = DEFAULT_DEXMAP_PATH,
    ledger_path: Optional[Path] = DEFAULT_LEDGER_PATH,
    limit: Optional[int] = None,
    template_id: str = "",
    timezone_name: Optional[str] = None,
    fill_only: frozenset = frozenset(),
    now: Optional[datetime] = None,
    tz: Optional[tzinfo] = None,
    today: Optional[date] = None,
    run_id: Optional[str] = None,
    stats: Optional[dict] = None,
) -> dict:
    """Run one sync and return the stats dict. Raises MapFileError, LedgerError,
    MappingAbort or NotionError (schema, query or a 401) without writing
    anything further. Pass a stats dict to keep the counts reached when it
    raises."""
    if stats is None:
        stats = new_stats(execute, max_writes, max_id_writes, max_creates)
    stats["fill_only"] = sorted(fill_only)
    stored_map = load_stored_map(map_path, strict=execute)
    ledger: Optional[Ledger] = None
    if execute:
        ledger = Ledger(Path(ledger_path), run_id or uuid.uuid4().hex[:12])
        stats["ledger_path"] = str(ledger.path)
    available = client.check_schema()
    if available is not None:
        for name in OPTIONAL_PROPERTIES:
            if name not in available and name != PROP_DEX_ID:
                logger.info("Property '%s' is not in the Notion schema; that field is skipped", name)
    rows = [parse_row(p) for p in client.query_rows(limit=limit)]

    # Pending creates: resolved once a row carries the LifeOS_ID, else reported.
    row_ids = {r.lifeos_id for r in rows if r.lifeos_id}
    pending = {k: v for k, v in stored_map.items() if isinstance(v, dict) and v.get("pending")}
    resolved = {k for k, v in pending.items() if v.get("lifeos_id") in row_ids}
    still_pending = {k: v for k, v in pending.items() if k not in resolved}
    stats["pending_creates"] = len(still_pending)
    for v in still_pending.values():
        runs = _int(v.get("runs")) or 1
        if execute:
            runs += 1
            v["runs"] = runs
        message = ("PENDING create for LifeOS %s (pending for %d runs): outcome unknown, not created "
                   "again until a row with that LifeOS_ID appears")
        if runs >= PENDING_CHECK_RUNS:
            logger.warning(message + ". Check Notion: if no page exists, delete the pending entry from "
                           "the stored mapping (see the guide).", v.get("lifeos_id"), runs)
        else:
            logger.warning(message, v.get("lifeos_id"), runs)

    plan = build_plan(
        rows, people, stored_map, load_dexmap(dexmap_path),
        now=now, tz=tz, today=today, available=available, fill_only=fill_only,
    )
    stats.update(
        rows=plan.rows, matched=plan.matched, unmatched=len(plan.unmatched),
        orphans=len(plan.orphans), duplicates=len(plan.duplicates),
        possible_matches=len(plan.possible_matches),
        changes=sum(1 for c in plan.changes if c.fields), conflicts=len(plan.conflicts),
        skipped_values=len(plan.skips), duplicate_name_skips=len(plan.duplicate_name_skips),
        possible_duplicate_skips=len(plan.possible_duplicate_skips),
    )
    check_mapping_ratio(plan)

    for row, reason in plan.unmatched:
        logger.info("UNMATCHED %s (%s): %s", row.name or "(no name)", row.page_id, reason)
    for row, person in plan.possible_matches:
        logger.warning("POSSIBLE MATCH, review by hand: Notion row %s (%s) may be LifeOS person %s (%s); "
                       "nothing written", row.name, row.page_id, person.get("display_name"), person.get("id"))
    for row, message in plan.conflicts:
        logger.warning("CONFLICT %s (%s): %s", row.name or "(no name)", row.page_id, message)
    for row, message in plan.skips:
        logger.info("SKIPPED %s (%s): %s", row.name or "(no name)", row.page_id, message)
    for person, why in plan.duplicate_name_skips + plan.possible_duplicate_skips:
        logger.warning("SKIPPED CREATE %s (%s): %s", person.get("display_name"), person.get("id"), why)

    applied, deferred_updates, deferred_ids = apply_caps(plan.changes, max_writes, max_id_writes)
    stats["deferred"] = deferred_updates
    stats["deferred_id_writes"] = deferred_ids
    stats["planned_id_writes"] = sum(1 for a in applied if a.with_id)
    if deferred_updates:
        logger.info("DEFERRED %d page updates over --max-writes %d", deferred_updates, max_writes)
    if deferred_ids:
        logger.info("DEFERRED %d LifeOS_ID writes over --max-id-writes %d", deferred_ids, max_id_writes)
    logger.info("Planned LifeOS_ID writes: %d", stats["planned_id_writes"])
    for item in applied:
        if not item.with_fields:
            continue
        for f in item.change.fields:
            if f.displaces:
                stats["displaced_values"] += 1
                logger.warning("DISPLACES a value LifeOS has never seen: %s (%s): %s: %s -> %s",
                               item.change.name, item.change.page_id, f.field, _short(f.old), _short(f.new))

    stamp = (today or date.today()).isoformat()
    if execute:
        assert ledger is not None
        if plan.new_mappings or resolved or still_pending:
            for key in resolved:
                stored_map.pop(key, None)
            stored_map.update(plan.new_mappings)
            save_map_atomic(Path(map_path), stored_map)
            stats["new_mappings"] = len(plan.new_mappings)
            logger.info("Stored %d new row mappings in %s", len(plan.new_mappings), map_path)
        for item in applied:
            if stats["updates_stopped"]:
                break
            change = item.change
            steps = []
            if item.with_id:
                steps.append(("id_write", [(PROP_LIFEOS_ID, None, change.id_value, None)],
                              change.properties(with_fields=False)))
            if item.with_fields:
                steps.append(("update", [(f.field, f.old, f.new, f.old_full) for f in change.fields],
                              change.properties(with_id=False)))
            for action, entries, properties in steps:
                try:
                    _patch_with_ledger(client, ledger, change.page_id, change.lifeos_id, action,
                                       entries, properties)
                except NotionError as exc:
                    if exc.status == 401:
                        raise
                    stats["failed"] += 1
                    logger.error("FAILED %s (%s): %s", change.name, change.page_id, exc)
                    if exc.status == 403:
                        stats["updates_stopped"] = f"403: {exc}"
                        logger.error("Updates stopped for this run after a 403")
                        break
                    continue
                if action == "id_write":
                    stats["id_writes"] += 1
                    logger.info("UPDATED %s (%s): LifeOS_ID -> %s", change.name, change.page_id,
                                change.id_value)
                else:
                    stats["writes"] += 1
                    for f in change.fields:
                        logger.info("UPDATED %s (%s): %s: %s -> %s", change.name, change.page_id,
                                    f.field, _short(f.old) or "(empty)", _short(f.new))
    else:
        for item in applied:
            _log_change("WOULD UPDATE", item)

    # New pages come last so a failure here never blocks the updates above.
    planned: list[dict] = []
    deferred_creates = 0
    if limit:
        logger.info("--limit is set: page creation skipped")
    elif not rows and plan.creates:
        logger.warning("No Notion rows were read; refusing to create pages")
    else:
        planned = plan.creates[: max(0, max_creates)]
        deferred_creates = len(plan.creates) - len(planned)
    stats["planned_creates"] = len(planned)
    stats["deferred_creates"] = deferred_creates
    if deferred_creates:
        logger.info("DEFERRED %d new pages over --max-creates %d", deferred_creates, max_creates)
    logger.info("Planned new pages: %d%s", len(planned),
                (": " + ", ".join(_short(p.get("display_name"), 30) for p in planned)) if planned else "")
    if execute and planned:
        assert ledger is not None
        for person in planned:
            try:
                _create_page_flow(client, person, template_id, timezone_name, now, tz, available,
                                  ledger, stored_map, Path(map_path), stamp, stats, fill_only)
            except NotionError as exc:
                if exc.status == 401:
                    raise
                stats["creates_stopped"] = f"{exc.status}: {exc}"
                logger.error("Page creation stopped for this run: %s", exc)
                break

    if stats["failed"]:
        stats["status"], stats["exit_code"] = "write_failures", EXIT_WRITE_FAILURES
    elif stats["creates_stopped"]:
        stats["status"], stats["exit_code"] = "creates_stopped", EXIT_WRITE_FAILURES
    elif stats["pending_creates"]:
        stats["status"], stats["exit_code"] = "pending_creates", EXIT_WRITE_FAILURES
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def setup_logging(log_path: Optional[Path], token: str = "") -> None:
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(fmt)
        handler.addFilter(RedactingFilter(token))
        logger.addHandler(handler)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="One-way sync from LifeOS to the Notion People data source "
                    "(contact fields, LifeOS_ID, new pages)"
    )
    p.add_argument("--execute", action="store_true", help="Apply changes (default is a dry run)")
    p.add_argument("--max-writes", type=int, default=DEFAULT_MAX_WRITES,
                   help="Maximum page updates for field changes per run (default %(default)s)")
    p.add_argument("--max-id-writes", type=int, default=DEFAULT_MAX_ID_WRITES,
                   help="Maximum LifeOS_ID writes per run (default %(default)s)")
    p.add_argument("--max-creates", type=int, default=DEFAULT_MAX_CREATES,
                   help="Maximum new pages per run (default %(default)s)")
    p.add_argument("--api-base", default=DEFAULT_API_BASE, help="LifeOS API base URL (default %(default)s)")
    p.add_argument("--data-source-id", default=None,
                   help="Notion People data source ID (default: LIFEOS_NOTION_PEOPLE_DATA_SOURCE)")
    p.add_argument("--map", dest="map_path", type=Path, default=DEFAULT_MAP_PATH,
                   help="Stored page mapping file (default %(default)s)")
    p.add_argument("--dexmap", dest="dexmap_path", type=Path, default=DEFAULT_DEXMAP_PATH,
                   help="Dex_ID seed mapping file (default %(default)s)")
    p.add_argument("--ledger", dest="ledger_path", type=Path, default=DEFAULT_LEDGER_PATH,
                   help="Change ledger written in execute mode (default %(default)s)")
    p.add_argument("--fill-only-fields", default=None,
                   help="Comma-separated Notion properties that may only fill an empty cell "
                        "(default: LIFEOS_NOTION_FILL_ONLY_FIELDS). Allowed: "
                        + ", ".join(FILL_ONLY_ALLOWED))
    p.add_argument("--limit", type=int, default=None,
                   help="Read at most this many Notion rows (page creation is skipped)")
    p.add_argument("--log-file", type=Path, default=DEFAULT_LOG_PATH,
                   help="Log file (default %(default)s)")
    p.add_argument("--no-file-log", action="store_true",
                   help="Log to stdout only (for callers that capture the output)")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    from config.settings import settings

    token = (settings.notion_token or "").strip()
    setup_logging(None if args.no_file_log else args.log_file, token)
    stats = new_stats(args.execute, args.max_writes, args.max_id_writes, args.max_creates)

    def finish(status: str, code: int) -> int:
        stats["status"], stats["exit_code"] = status, code
        print("SYNC_STATS:" + json.dumps(stats, sort_keys=True), flush=True)
        return code

    if not token:
        logger.error("LIFEOS_NOTION_TOKEN is not set. Put the Notion integration token in .env; "
                     "see docs/guides/notion-people-sync.md.")
        return finish("config_error", EXIT_CONFIG)
    try:
        fill_only = parse_fill_only(
            args.fill_only_fields if args.fill_only_fields is not None
            else settings.notion_fill_only_fields
        )
    except ConfigError as exc:
        logger.error("%s", exc)
        return finish("config_error", EXIT_CONFIG)
    data_source_id = args.data_source_id or settings.notion_people_data_source_id

    client = NotionClient(token, data_source_id, version=settings.notion_version)
    try:
        logger.info("Notion People sync (%s)", "execute" if args.execute else "dry run")
        people = fetch_people(args.api_base)
        logger.info("LifeOS people: %d", len(people))
        run_sync(
            client, people,
            execute=args.execute, max_writes=args.max_writes,
            max_id_writes=args.max_id_writes, max_creates=args.max_creates,
            map_path=args.map_path, dexmap_path=args.dexmap_path,
            ledger_path=args.ledger_path, limit=args.limit,
            template_id=(settings.notion_person_template_id or "").strip(),
            timezone_name=local_timezone_name(),
            fill_only=fill_only,
            stats=stats,
        )
    except (LifeOSUnavailable, NotionError, MapFileError, LedgerError) as exc:
        logger.error("%s", exc)
        return finish("config_error", EXIT_CONFIG)
    except MappingAbort as exc:
        logger.error("ABORTED: %s", exc)
        return finish("mapping_abort", EXIT_MAPPING_ABORT)
    except (ValueError, OSError) as exc:
        logger.error("Unexpected %s: %s", type(exc).__name__, exc)
        return finish("error", EXIT_CONFIG)
    finally:
        client.close()

    logger.info(
        "Rows %d, matched %d, unmatched %d (orphans %d, duplicates %d, possible matches %d), "
        "pages with field changes %d, updated %d, LifeOS_ID written %d, created %d, pending creates %d, "
        "deferred %d/%d/%d, displaced values %d, failed %d",
        stats["rows"], stats["matched"], stats["unmatched"], stats["orphans"], stats["duplicates"],
        stats["possible_matches"], stats["changes"], stats["writes"], stats["id_writes"],
        stats["creates"], stats["pending_creates"], stats["deferred"], stats["deferred_id_writes"],
        stats["deferred_creates"], stats["displaced_values"], stats["failed"],
    )
    if not args.execute and (stats["changes"] or stats["planned_id_writes"] or stats["planned_creates"]):
        logger.info("Dry run: nothing written. Re-run with --execute to apply.")
    return finish(stats["status"], stats["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
