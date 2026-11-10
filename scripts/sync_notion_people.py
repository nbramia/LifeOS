#!/usr/bin/env python3
"""
One-way sync from LifeOS to the Notion "People" database.

For each Notion row that maps to a LifeOS person, three properties are filled
in from LifeOS and nothing else is touched:

  * Email         set from the person's first email when the Notion cell is empty
  * Phone         set from the person's first phone number when the cell is empty
  * Last Check-In set to the local date of the person's last_seen when the cell
                  is empty or older (never moved backwards, never in the future)

A filled Email or Phone is never overwritten. When Notion holds a value that
LifeOS does not know, the row is reported as a CONFLICT and left alone. No
pages are created, archived or deleted and the database schema is not changed.

Dry run unless --execute. Re-running is safe: a second run finds nothing to do.
--max-writes caps page updates per run (applied in name order, the rest are
reported as deferred). The run aborts without writing when fewer than half of
the Notion rows can be matched to a LifeOS person.

Notion row -> LifeOS person is resolved in this order:
  1. data/notion_people_map.json    stored mapping, written after a successful
                                    --execute run for newly resolved rows
  2. data/notion_people_dexmap.json seed keyed by the row's Dex_ID text
  3. identifiers                    Email, Phone (last 10 digits), LinkedIn URL;
                                    accepted only when exactly one person matches
Every match must also pass a first/last name check.

Exit status: 0 success, 1 some page updates failed, 2 configuration, LifeOS API,
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
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, tzinfo
from typing import Any, Callable, Optional

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_API_BASE = "http://127.0.0.1:8000"
DEFAULT_MAP_PATH = PROJECT_ROOT / "data" / "notion_people_map.json"
DEFAULT_DEXMAP_PATH = PROJECT_ROOT / "data" / "notion_people_dexmap.json"
DEFAULT_LOG_PATH = PROJECT_ROOT / "logs" / "notion-people-sync.log"

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
PACE_SECONDS = 0.35  # about 3 requests per second
MAX_RETRIES = 4
MAX_RETRY_WAIT = 60.0
DEFAULT_MAX_WRITES = 50
MIN_MATCH_RATIO = 0.5

PROP_NAME = "Full Name"
PROP_DEX_ID = "Dex_ID"
PROP_EMAIL = "Email"
PROP_PHONE = "Phone"
PROP_LINKEDIN = "LinkedIn"
PROP_CHECKIN = "Last Check-In"

# Notion calls a text property "rich_text". LinkedIn is read only for matching,
# so a database without it still syncs.
REQUIRED_PROPERTIES = {
    PROP_NAME: "title",
    PROP_DEX_ID: "rich_text",
    PROP_EMAIL: "email",
    PROP_PHONE: "phone_number",
    PROP_LINKEDIN: "url",
    PROP_CHECKIN: "date",
}
OPTIONAL_PROPERTIES = {PROP_LINKEDIN}

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

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class LifeOSUnavailable(RuntimeError):
    """The LifeOS API could not be read."""


class MapFileError(RuntimeError):
    """The stored mapping file exists but cannot be read."""


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


def normalize_linkedin(url: Optional[str]) -> Optional[str]:
    """Lowercase, no scheme, www., query, fragment or trailing slash.

    Returns None unless the result contains linkedin.com/in/.
    """
    if not url:
        return None
    s = url.strip().lower()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = re.split(r"[?#]", s, maxsplit=1)[0]
    s = _LINKEDIN_COUNTRY_HOST.sub("linkedin.com", s).rstrip("/")
    return s if "linkedin.com/in/" in s else None


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


# ---------------------------------------------------------------------------
# Notion rows
# ---------------------------------------------------------------------------

@dataclass
class NotionRow:
    page_id: str
    name: str = ""
    dex_id: str = ""
    email: Optional[str] = None
    phone: Optional[str] = None
    linkedin: Optional[str] = None
    last_checkin: Optional[str] = None  # YYYY-MM-DD (or whatever Notion held)


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
        email=(prop(PROP_EMAIL).get("email") or None),
        phone=(prop(PROP_PHONE).get("phone_number") or None),
        linkedin=(prop(PROP_LINKEDIN).get("url") or None),
        last_checkin=(str(start)[:10] if start else None),
    )


# ---------------------------------------------------------------------------
# Mapping files
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


# ---------------------------------------------------------------------------
# Resolving Notion rows to LifeOS people
# ---------------------------------------------------------------------------

@dataclass
class Resolution:
    person: Optional[dict] = None
    matched_by: Optional[str] = None  # dex_map | identifiers
    source: Optional[str] = None      # map | dexmap | identifiers
    reason: Optional[str] = None      # why unmatched


@dataclass
class PeopleIndex:
    by_id: dict[str, dict] = field(default_factory=dict)
    by_email: dict[str, set] = field(default_factory=dict)
    by_phone: dict[str, set] = field(default_factory=dict)
    by_linkedin: dict[str, set] = field(default_factory=dict)


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
        link = normalize_linkedin(person.get("linkedin_url"))
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
    """Resolve one row: stored map, then Dex_ID seed, then identifiers."""
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

@dataclass
class Change:
    page_id: str
    name: str
    lifeos_id: str
    properties: dict          # Notion write payload, changed properties only
    fields: list[str]         # human labels: Email, Phone, Last Check-In


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


def compute_changes(
    row: NotionRow,
    person: dict,
    now: Optional[datetime] = None,
    tz: Optional[tzinfo] = None,
) -> tuple[Optional[Change], list[str]]:
    """What to write for one matched row, plus CONFLICT messages (no writes)."""
    props: dict = {}
    fields: list[str] = []
    conflicts: list[str] = []

    emails = _str_list(person.get("emails"))
    if not row.email:
        if emails:
            props[PROP_EMAIL] = {"email": emails[0]}
            fields.append(PROP_EMAIL)
    elif normalize_email(row.email) not in {normalize_email(e) for e in emails}:
        conflicts.append(
            f"Email: Notion has {row.email}, LifeOS knows "
            f"{', '.join(emails) if emails else 'no email'}"
        )

    phones = _str_list(person.get("phone_numbers"))
    if not row.phone:
        if phones:
            props[PROP_PHONE] = {"phone_number": phones[0]}
            fields.append(PROP_PHONE)
    elif last10(row.phone) not in {last10(p) for p in phones if last10(p)}:
        conflicts.append(
            f"Phone: Notion has {row.phone}, LifeOS knows "
            f"{', '.join(phones) if phones else 'no phone'}"
        )

    seen = last_seen_local_date(person.get("last_seen"), now=now, tz=tz)
    if seen is not None:
        current: Optional[date] = None
        usable = True
        if row.last_checkin:
            try:
                current = date.fromisoformat(row.last_checkin[:10])
            except ValueError:
                usable = False
                conflicts.append(f"Last Check-In: unreadable Notion date '{row.last_checkin}'")
        if usable and (current is None or current < seen):
            props[PROP_CHECKIN] = {"date": {"start": seen.isoformat()}}
            fields.append(PROP_CHECKIN)

    change = (
        Change(row.page_id, row.name, str(person.get("id")), props, fields) if props else None
    )
    return change, conflicts


def apply_cap(changes: list[Change], max_writes: int) -> tuple[list[Change], list[Change]]:
    """Order changes by name and split into (to apply, deferred)."""
    ordered = sorted(changes, key=lambda c: (c.name.casefold(), c.page_id))
    cap = max(0, max_writes)
    return ordered[:cap], ordered[cap:]


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    rows: int = 0
    matched: int = 0
    unmatched: list[tuple[NotionRow, str]] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    conflicts: list[tuple[NotionRow, str]] = field(default_factory=list)
    new_mappings: dict[str, dict] = field(default_factory=dict)


def build_plan(
    rows: list[NotionRow],
    people: list[dict],
    stored_map: dict[str, dict],
    dexmap: dict[str, str],
    now: Optional[datetime] = None,
    tz: Optional[tzinfo] = None,
    today: Optional[date] = None,
) -> Plan:
    index = build_index(people)
    plan = Plan(rows=len(rows))
    stamp = (today or date.today()).isoformat()
    for row in rows:
        res = resolve_row(row, index, stored_map, dexmap)
        if res.person is None:
            plan.unmatched.append((row, res.reason or "unmatched"))
            continue
        plan.matched += 1
        if res.source != "map":
            plan.new_mappings[row.page_id] = {
                "lifeos_id": str(res.person["id"]),
                "dex_id": row.dex_id,
                "matched_by": res.matched_by,
                "added": stamp,
            }
        change, conflicts = compute_changes(row, res.person, now=now, tz=tz)
        if change:
            plan.changes.append(change)
        plan.conflicts.extend((row, c) for c in conflicts)
    return plan


def check_mapping_ratio(plan: Plan) -> None:
    """Raise MappingAbort when under half of the Notion rows matched."""
    if plan.rows and plan.matched / plan.rows < MIN_MATCH_RATIO:
        raise MappingAbort(
            f"only {plan.matched} of {plan.rows} Notion rows matched a LifeOS person "
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
        database_id: str,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
        pace: float = PACE_SECONDS,
        max_retries: int = MAX_RETRIES,
        base_url: str = NOTION_API,
    ):
        self._token = token
        self.database_id = database_id
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
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._http.close()

    def _fail(self, message: str, status: Optional[int] = None) -> NotionError:
        return NotionError(redact(message, self._token), status)

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
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
                if attempt >= self._max_retries:
                    raise self._fail(f"Notion request failed: {type(exc).__name__}: {exc}")
                self._sleep(min(2.0 ** attempt, 30.0))
                continue

            status = resp.status_code
            if status < 300:
                try:
                    return resp.json()
                except ValueError:
                    raise self._fail(f"Notion returned HTTP {status} with a body that is not JSON", status)
            if status in (409, 429) or status >= 500:
                if attempt >= self._max_retries:
                    raise self._fail(f"Notion returned HTTP {status} after {attempt + 1} attempts", status)
                delay = min(2.0 ** attempt, 30.0)
                if status == 429:
                    try:
                        delay = float(resp.headers.get("Retry-After", delay))
                    except ValueError:
                        pass
                self._sleep(min(max(delay, 0.0), MAX_RETRY_WAIT))
                continue
            if status == 401:
                raise self._fail("Notion rejected the token (HTTP 401): check LIFEOS_NOTION_TOKEN", status)
            if status in (403, 404):
                raise self._fail(
                    f"Notion returned HTTP {status}: the database or page is not shared with the "
                    "integration (database menu > Connections), or the ID is wrong",
                    status,
                )
            raise self._fail(f"Notion returned HTTP {status}: {resp.text[:200]}", status)
        raise self._fail("Notion request failed")  # pragma: no cover

    def check_schema(self) -> None:
        """Verify the database has the properties the sync reads and writes,
        with the expected Notion types. LinkedIn is optional."""
        data = self._request("GET", f"/databases/{self.database_id}")
        found = data.get("properties")
        found = found if isinstance(found, dict) else {}
        for name, expected in REQUIRED_PROPERTIES.items():
            if name not in found:
                if name in OPTIONAL_PROPERTIES:
                    continue
                raise self._fail(
                    f"Notion database has no property '{name}' (expected type {expected}); "
                    f"properties found: {', '.join(sorted(found)) or 'none'}"
                )
            actual = (found[name] or {}).get("type")
            if actual != expected:
                raise self._fail(
                    f"Notion property '{name}' has type {actual}, expected {expected}"
                )

    def query_rows(self, limit: Optional[int] = None) -> list[dict]:
        """All pages of the People database (following next_cursor)."""
        pages: list[dict] = []
        cursor: Optional[str] = None
        while True:
            payload: dict = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data = self._request("POST", f"/databases/{self.database_id}/query", payload)
            pages.extend(data.get("results") or [])
            if limit and len(pages) >= limit:
                return pages[:limit]
            if not data.get("has_more") or not data.get("next_cursor"):
                return pages
            cursor = data["next_cursor"]

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self._request("PATCH", f"/pages/{page_id}", {"properties": properties})


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

def new_stats(execute: bool, max_writes: int) -> dict:
    return {
        "mode": "execute" if execute else "dry-run",
        "status": "ok",
        "exit_code": EXIT_OK,
        "rows": 0,
        "matched": 0,
        "unmatched": 0,
        "changes": 0,
        "writes": 0,
        "failed": 0,
        "conflicts": 0,
        "deferred": 0,
        "new_mappings": 0,
        "max_writes": max_writes,
    }


def run_sync(
    client: Any,
    people: list[dict],
    *,
    execute: bool = False,
    max_writes: int = DEFAULT_MAX_WRITES,
    map_path: Optional[Path] = DEFAULT_MAP_PATH,
    dexmap_path: Optional[Path] = DEFAULT_DEXMAP_PATH,
    limit: Optional[int] = None,
    now: Optional[datetime] = None,
    tz: Optional[tzinfo] = None,
    today: Optional[date] = None,
    stats: Optional[dict] = None,
) -> dict:
    """Run one sync and return the stats dict. Raises MapFileError, MappingAbort
    or NotionError (schema, query or a 401 on update) without writing anything
    further. Pass a stats dict to keep the counts reached when it raises."""
    if stats is None:
        stats = new_stats(execute, max_writes)
    stored_map = load_stored_map(map_path, strict=execute)
    client.check_schema()
    rows = [parse_row(p) for p in client.query_rows(limit=limit)]
    plan = build_plan(
        rows, people, stored_map, load_dexmap(dexmap_path),
        now=now, tz=tz, today=today,
    )
    stats.update(
        rows=plan.rows, matched=plan.matched, unmatched=len(plan.unmatched),
        changes=len(plan.changes), conflicts=len(plan.conflicts),
    )
    check_mapping_ratio(plan)

    for row, reason in plan.unmatched:
        logger.info("UNMATCHED %s (%s): %s", row.name or "(no name)", row.page_id, reason)
    for row, message in plan.conflicts:
        logger.warning("CONFLICT %s (%s): %s", row.name or "(no name)", row.page_id, message)

    to_apply, deferred = apply_cap(plan.changes, max_writes)
    stats["deferred"] = len(deferred)
    for change in deferred:
        logger.info("DEFERRED %s (%s): %s", change.name, change.page_id, ", ".join(change.fields))

    if execute:
        if plan.new_mappings:
            stored_map.update(plan.new_mappings)
            save_map_atomic(Path(map_path), stored_map)
            stats["new_mappings"] = len(plan.new_mappings)
            logger.info("Stored %d new row mappings in %s", len(plan.new_mappings), map_path)
        for change in to_apply:
            try:
                client.update_page(change.page_id, change.properties)
            except NotionError as exc:
                if exc.status == 401:
                    raise
                stats["failed"] += 1
                logger.error("FAILED %s (%s): %s", change.name, change.page_id, exc)
                continue
            stats["writes"] += 1
            logger.info("UPDATED %s (%s): %s", change.name, change.page_id, ", ".join(change.fields))
    else:
        for change in to_apply:
            logger.info("WOULD UPDATE %s (%s): %s", change.name, change.page_id, ", ".join(change.fields))

    if stats["failed"]:
        stats["status"], stats["exit_code"] = "write_failures", EXIT_WRITE_FAILURES
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
        description="One-way sync of Email, Phone and Last Check-In from LifeOS to the Notion People database"
    )
    p.add_argument("--execute", action="store_true", help="Apply changes (default is a dry run)")
    p.add_argument("--max-writes", type=int, default=DEFAULT_MAX_WRITES,
                   help="Maximum page updates per run (default %(default)s)")
    p.add_argument("--api-base", default=DEFAULT_API_BASE, help="LifeOS API base URL (default %(default)s)")
    p.add_argument("--database-id", default=None,
                   help="Notion People database ID (default: LIFEOS_NOTION_PEOPLE_DB)")
    p.add_argument("--map", dest="map_path", type=Path, default=DEFAULT_MAP_PATH,
                   help="Stored page mapping file (default %(default)s)")
    p.add_argument("--dexmap", dest="dexmap_path", type=Path, default=DEFAULT_DEXMAP_PATH,
                   help="Dex_ID seed mapping file (default %(default)s)")
    p.add_argument("--limit", type=int, default=None, help="Read at most this many Notion rows")
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
    stats = new_stats(args.execute, args.max_writes)

    def finish(status: str, code: int) -> int:
        stats["status"], stats["exit_code"] = status, code
        print("SYNC_STATS:" + json.dumps(stats, sort_keys=True), flush=True)
        return code

    if not token:
        logger.error("LIFEOS_NOTION_TOKEN is not set. Put the Notion integration token in .env; "
                     "see docs/guides/notion-people-sync.md.")
        return finish("config_error", EXIT_CONFIG)
    database_id = args.database_id or settings.notion_people_database_id

    client = NotionClient(token, database_id)
    try:
        logger.info("Notion People sync (%s)", "execute" if args.execute else "dry run")
        people = fetch_people(args.api_base)
        logger.info("LifeOS people: %d", len(people))
        run_sync(
            client, people,
            execute=args.execute, max_writes=args.max_writes,
            map_path=args.map_path, dexmap_path=args.dexmap_path, limit=args.limit,
            stats=stats,
        )
    except (LifeOSUnavailable, NotionError, MapFileError) as exc:
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
        "Rows %d, matched %d, unmatched %d, pages with changes %d, updated %d, "
        "conflicts %d, deferred %d, failed %d",
        stats["rows"], stats["matched"], stats["unmatched"], stats["changes"],
        stats["writes"], stats["conflicts"], stats["deferred"], stats["failed"],
    )
    if not args.execute and stats["changes"]:
        logger.info("Dry run: nothing written. Re-run with --execute to apply.")
    return finish(stats["status"], stats["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
