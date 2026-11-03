"""Find a person in bulk-content lists through the keyword index, only when the owner asks.

Nothing is stored on the person, and row text is personal data: it goes to the caller and is
never logged. No chat turn, person block or tool calls this.

A long note is indexed with its line breaks collapsed, so a table's rows run together and a
row boundary reads as an empty cell. The row reported is the run of filled cells around the
match: it may stop short of the row's end, and it never runs into the next row.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from api.services.bm25_index import BM25Index
    from api.services.person_entity import PersonEntity

MAX_MATCHES = 50
# Matching chunks read per email, phone or name, so a common name can't read a whole list.
MAX_CHUNKS_PER_LOOKUP = 400
# Characters kept on each side of a match in a row.
ROW_REACH = 300

_EMPTY_CELL = re.compile(r"\|\s*\|")


@dataclass
class ListMatch:
    """A list row that matches a person."""

    list_name: str
    row: str
    match_type: str  # "email", "phone" or "name"


def _email_lookup(email: str) -> tuple[list[str], re.Pattern]:
    return [email], re.compile(rf"(?<![\w.+\-]){re.escape(email)}(?![\w\-]|\.\w)", re.IGNORECASE)


def _phone_lookup(phone: str) -> Optional[tuple[list[str], re.Pattern]]:
    """Phrases and a pattern for a phone number: a US number in its written forms, others whole."""
    digits = re.sub(r"\D", "", phone)
    national = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
    if len(national) == 10:
        area, exchange, line = national[:3], national[3:6], national[6:]
        separator = r"[\s.\-]*"
        pattern = rf"(?:\+?1{separator})?\(?{area}\)?{separator}{exchange}{separator}{line}"
        return [f"{area} {exchange} {line}", national, f"1{national}"], re.compile(rf"(?<!\d){pattern}(?!\d)")
    if len(digits) >= 7:
        return [digits], re.compile(rf"(?<!\d)\+?{digits}(?!\d)")
    return None


def _name_lookup(name: str) -> Optional[tuple[list[str], re.Pattern]]:
    """Phrases and a pattern for a full name: "First Last", "Last, First" or adjacent cells."""
    parts = name.split()
    if len(parts) < 2 or re.search(r"[\d@]", name):
        return None
    forward = r"(?:\s+|\s*\|\s*)".join(map(re.escape, parts))
    reverse = re.escape(parts[-1]) + r",\s*" + r"\s+".join(map(re.escape, parts[:-1]))
    phrases = [" ".join(parts), " ".join([parts[-1], *parts[:-1]])]
    return phrases, re.compile(rf"(?<!\w)(?:{forward}|{reverse})(?!\w)", re.IGNORECASE)


def _row_around(body: str, start: int, end: int) -> str:
    """The run of filled cells (or the line, in plain text) around a match."""
    left = max(start - ROW_REACH, body.rfind("\n", 0, start) + 1)
    for cell in _EMPTY_CELL.finditer(body, left, start):
        left = cell.end() - 1
    right = min(end + ROW_REACH, len(body))
    newline = body.find("\n", end, right)
    if newline >= 0:
        right = newline
    cell = _EMPTY_CELL.search(body, end, right)
    if cell:
        right = cell.start() + 1
    return " ".join(body[left:right].split()).strip("| ")


def find_list_matches(
    person: "PersonEntity", index: "BM25Index", is_bulk: Callable[[str], bool]
) -> tuple[list[ListMatch], bool]:
    """
    Look a person up in bulk-content lists by exact email or phone, then exact full name.

    Args:
        person: The person to look up
        index: Keyword index to read
        is_bulk: Test for a bulk chunk id

    Returns:
        (matches, truncated): email matches, then phone, then name, with no row
        repeated. ``truncated`` is True when more may exist.
    """
    lookups = [("email", _email_lookup(email)) for email in person.emails if email]
    lookups += [("phone", _phone_lookup(phone)) for phone in person.phone_numbers if phone]
    lookups.append(("name", _name_lookup(person.canonical_name or "")))

    matches: list[ListMatch] = []
    truncated = False
    for match_type, lookup in lookups:
        if lookup is None:
            continue
        phrases, pattern = lookup
        chunks = index.chunks_with_phrases(
            phrases, lambda doc_id: is_bulk(doc_id) and not doc_id.endswith("::summary"), MAX_CHUNKS_PER_LOOKUP
        )
        truncated = truncated or len(chunks) >= MAX_CHUNKS_PER_LOOKUP
        for chunk in chunks:
            # Before the first blank line is the contextual prefix, which only repeats the note's name.
            body = chunk["content"].partition("\n\n")[2]
            list_name = chunk["file_name"].removesuffix(".md")
            for found in pattern.finditer(body):
                row = _row_around(body, found.start(), found.end())
                if any(m.list_name == list_name and (row in m.row or m.row in row) for m in matches):
                    continue
                if len(matches) >= MAX_MATCHES:
                    return matches, True
                matches.append(ListMatch(list_name, row, match_type))
    return matches, truncated
