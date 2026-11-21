"""Read social and contact columns from Dex and Notion CSV exports.

Columns are recognized from their headers, so an export's own column set is
what drives parsing: platform columns come from the registry's
``export_columns``, and email/phone columns may carry a label in the header
(``Work Email``), in a paired column (``Phone 1 - Label``), or in the cell
(``work: name@example.com``). Rows enrich existing people only; a row that
matches nobody, or matches two different people, is counted and left alone.
"""
import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from api.services.person_entity import PersonEntity, PersonEntityStore
from api.services.phone_utils import normalize_phone
from api.services.social_links import (
    InvalidContactDetail,
    build_social_link,
    normalize_email,
    normalize_label,
    parse_social_value,
    social_identity,
)
from config.social_platforms import SOCIAL_PLATFORMS

_KIND_RE = re.compile(
    r"^(?:(?P<pre>.*?) )?"
    r"(?P<kind>e ?mails?(?: address(?:es)?)?|phones?(?: numbers?)?|telephone|mobile|cell(?:phone)?)"
    r"(?: (?P<n>\d+))?(?: (?P<post>.+))?$"
)
# Header words that say where a column sits rather than what kind of address it holds.
_POSITIONAL_WORDS = {"primary", "secondary", "main", "default", "contact", "value", "address", "number"}
_SOCIAL_NOISE_WORDS = {"url", "profile", "handle", "link", "username", "user", "id", "page", "account"}
# Header words marking metadata about an address rather than an address.
_METADATA_WORDS = {
    "last", "first", "next", "count", "call", "calls", "date", "domain", "status",
    "verified", "opt", "out", "bounced", "sent", "received", "owner",
}
_LABEL_ROLES = {"type", "label"}
_CELL_SPLIT_RE = re.compile(r"\s*(?::::|[,;\n])\s*")
_LABEL_PREFIX_RE = re.compile(r"^([^:/@]{1,40}):\s+(.+)$")
_LABEL_SUFFIX_RE = re.compile(r"^(.+?)\s+\(([^)]{1,40})\)$")


def _normalize_header(header: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (header or "").lower()).split())


def _label_from_words(text: Optional[str], noise: set[str]) -> Optional[str]:
    words = [w for w in (text or "").split() if w not in noise and not w.isdigit()]
    return " ".join(words) or None


@dataclass
class _ValueColumn:
    header: str
    kind: str  # "email", "phone", or a platform key
    label: Optional[str] = None
    label_header: Optional[str] = None


@dataclass
class ColumnMap:
    """Which columns of an export hold names, addresses, and accounts."""

    name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    values: list[_ValueColumn] = field(default_factory=list)


def _match_social_column(normalized: str) -> Optional[tuple[str, Optional[str]]]:
    aliases = sorted(
        ((alias, p.key) for p in SOCIAL_PLATFORMS for alias in p.export_columns),
        key=lambda pair: -len(pair[0]),
    )
    for alias, platform in aliases:
        if normalized == alias:
            return platform, None
        if len(alias) > 2 and normalized.startswith(f"{alias} "):
            return platform, _label_from_words(normalized[len(alias) + 1:], _SOCIAL_NOISE_WORDS)
    return None


def map_columns(headers: Iterable[str]) -> ColumnMap:
    """Classify an export's headers."""
    result = ColumnMap()
    label_columns: dict[tuple[str, Optional[str]], str] = {}
    pending: list[tuple[_ValueColumn, Optional[str]]] = []

    for header in headers:
        normalized = _normalize_header(header)
        if normalized in ("name", "full name"):
            result.name = result.name or header
            continue
        if normalized in ("first name", "given name"):
            result.first_name = header
            continue
        if normalized in ("last name", "family name", "surname"):
            result.last_name = header
            continue

        social = _match_social_column(normalized)
        if social:
            result.values.append(_ValueColumn(header=header, kind=social[0], label=social[1]))
            continue

        match = _KIND_RE.match(normalized)
        if not match:
            continue
        words = set(f"{match['pre'] or ''} {match['post'] or ''}".split())
        if words & _METADATA_WORDS:
            continue
        raw_kind = match["kind"]
        kind = "email" if raw_kind.startswith("e") else "phone"
        if match["post"] in _LABEL_ROLES:
            label_columns[(kind, match["n"])] = header
            continue
        label = _label_from_words(
            " ".join(filter(None, [match["pre"], match["post"]])), _POSITIONAL_WORDS
        )
        if raw_kind in ("mobile", "cell", "cellphone") and not label:
            label = raw_kind
        pending.append((_ValueColumn(header=header, kind=kind, label=label), match["n"]))

    for column, number in pending:
        column.label_header = label_columns.get((column.kind, number))
        result.values.append(column)
    return result


@dataclass(frozen=True)
class ExportValue:
    """One address or account read from a row."""

    kind: str
    value: str
    label: Optional[str]


def _clean_exported_label(text: Optional[str]) -> Optional[str]:
    """Strip the ``*`` marker Google-format exports put on labels."""
    return (text or "").replace("*", " ").strip() or None


def _split_cell(cell: str) -> list[tuple[str, Optional[str]]]:
    items = []
    for part in _CELL_SPLIT_RE.split(cell.strip()):
        if not part:
            continue
        prefix = _LABEL_PREFIX_RE.match(part)
        suffix = _LABEL_SUFFIX_RE.match(part)
        if prefix:
            items.append((prefix[2].strip(), prefix[1].strip()))
        elif suffix:
            items.append((suffix[1].strip(), suffix[2].strip()))
        else:
            items.append((part, None))
    return items


def read_row_values(row: dict, columns: ColumnMap) -> list[ExportValue]:
    """Extract every address and account from one row."""
    values: list[ExportValue] = []
    for column in columns.values:
        cell = row.get(column.header) or ""
        if not cell.strip():
            continue
        items = _split_cell(cell)
        paired = _CELL_SPLIT_RE.split((row.get(column.label_header) or "").strip()) if column.label_header else []
        for index, (value, cell_label) in enumerate(items):
            paired_label = paired[index] if len(items) == len(paired) else (paired[0] if len(paired) == 1 else None)
            label = cell_label or _clean_exported_label(paired_label) or column.label
            values.append(ExportValue(column.kind, value, label))
    return values


def _row_name(row: dict, columns: ColumnMap) -> str:
    if columns.name and (row.get(columns.name) or "").strip():
        return " ".join(row[columns.name].split())
    first = (row.get(columns.first_name) or "") if columns.first_name else ""
    last = (row.get(columns.last_name) or "") if columns.last_name else ""
    return " ".join(f"{first} {last}".split())


def _safe_label(label: Optional[str]) -> Optional[str]:
    try:
        return normalize_label(label)
    except InvalidContactDetail:
        return None


class _Normalized:
    """Validated values of a row, ready for matching and applying."""

    def __init__(self, values: list[ExportValue], stats: Counter):
        self.emails: list[tuple[str, Optional[str]]] = []
        self.phones: list[tuple[str, Optional[str]]] = []
        self.socials: list[tuple[str, str, Optional[str]]] = []
        for item in values:
            label = _safe_label(item.label)
            try:
                if item.kind == "email":
                    self.emails.append((normalize_email(item.value), label))
                elif item.kind == "phone":
                    phone = normalize_phone(item.value)
                    if not phone:
                        raise InvalidContactDetail("Not a valid phone number")
                    self.phones.append((phone, label))
                else:
                    parse_social_value(item.kind, item.value)
                    self.socials.append((item.kind, item.value, label))
            except InvalidContactDetail:
                stats["invalid_values"] += 1


def _find_match(
    store: PersonEntityStore,
    row: _Normalized,
    name: str,
    stats: Counter,
) -> Optional[PersonEntity]:
    found: dict[str, tuple[PersonEntity, str]] = {}
    for platform, value, _label in row.socials:
        person = store.get_by_social_link(platform, value)
        if person:
            found.setdefault(person.id, (person, "social"))
    for email, _label in row.emails:
        person = store.get_by_email(email)
        if person and not person.hidden:
            found.setdefault(person.id, (person, "email"))
    for phone, _label in row.phones:
        person = store.get_by_phone(phone)
        if person and not person.hidden:
            found.setdefault(person.id, (person, "phone"))

    if len(found) > 1:
        stats["rows_conflicting"] += 1
        return None
    if found:
        person, how = next(iter(found.values()))
        stats[f"matched_by_{how}"] += 1
        return person
    if len(name.split()) >= 2:
        person = store.get_by_name(name)
        if person and not person.hidden:
            stats["matched_by_name"] += 1
            return person
    stats["rows_unmatched"] += 1
    return None


def _apply_row(
    store: PersonEntityStore,
    person: PersonEntity,
    row: _Normalized,
    source: str,
    stats: Counter,
) -> bool:
    changed = False
    for email, label in row.emails:
        if not person.has_email(email):
            if store.is_blocked(email):
                stats["blocked_values"] += 1
                continue
            person.add_email(email)
            stats["emails_added"] += 1
            changed = True
        changed = _fill_contact_label(person, "email", email, label, stats) or changed

    for phone, label in row.phones:
        if not person.has_phone(phone):
            if store.is_blocked(phone):
                stats["blocked_values"] += 1
                continue
            person.add_phone(phone)
            stats["phones_added"] += 1
            changed = True
        changed = _fill_contact_label(person, "phone", phone, label, stats) or changed

    for platform, value, label in row.socials:
        link = build_social_link(platform, value, label, source=source)
        identity = social_identity(link)
        if identity in person.dismissed_social_keys:
            continue
        existing = next((item for item in person.social_links if social_identity(item) == identity), None)
        if existing is None:
            person.add_social_link(link)
            stats["links_added"] += 1
            changed = True
        elif link["label"] and not existing.get("label"):
            existing["label"] = link["label"]
            stats["labels_filled"] += 1
            changed = True
    return changed


def _fill_contact_label(
    person: PersonEntity,
    kind: str,
    value: str,
    label: Optional[str],
    stats: Counter,
) -> bool:
    person.reconcile_contact_details()
    entries = person.email_entries if kind == "email" else person.phone_entries
    entry = next(e for e in entries if e["value"].lower() == value.lower())
    if label and not entry.get("label"):
        entry["label"] = label
        stats["labels_filled"] += 1
        return True
    return False


def import_contact_export(
    path: Path,
    source: str,
    store: PersonEntityStore,
    execute: bool = False,
) -> dict:
    """Merge one CSV export's social and contact columns into existing people.

    Args:
        path: CSV file to read.
        source: Provenance recorded on added links, e.g. "dex" or "notion".
        store: Person store to match against and update.
        execute: Persist changes; a dry run only counts them.

    Returns:
        Aggregate counts, containing no personal data.
    """
    stats: Counter = Counter()
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = map_columns(reader.fieldnames or [])
        stats["columns_recognized"] = len(columns.values)
        for row in reader:
            stats["rows_read"] += 1
            normalized = _Normalized(read_row_values(row, columns), stats)
            match = _find_match(store, normalized, _row_name(row, columns), stats)
            if match is None:
                continue
            person = store.get_by_id(match.id)
            if _apply_row(store, person, normalized, source, stats):
                stats["people_updated"] += 1
                if execute:
                    store.update(person)
    return dict(stats)
