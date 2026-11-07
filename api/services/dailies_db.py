"""
Dailies database source for the Daily Journal feed.

Reads check-ins from Home's Postgres table `home_dailies` as the read-only
role `home_dailies_reader` and turns each row into a `JournalEntry` keyed by
the Dailies Google Form's question titles, so `GSheetSyncService` writes the
same `Personal/Journal/YYYY-MM-DD.md` note a sheet row produces.

The connection string comes from `LIFEOS_DAILIES_DATABASE_URL` and is never
committed. Logs carry fixed categories only, never entry content.
"""
import hashlib
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from api.services.gsheet_sync import JournalEntry, SheetConfig

SOURCE_ID = "home_dailies"
JOURNAL_FOLDER = "Personal/Journal"

# Never selects the internal id, imported raw rows or sheet-mirror state, so
# none of them can reach a note. `subject` (to confirm the table holds a
# single member's entries), `source` and `revision` (see `is_home_authored`)
# are read for those checks only and never mapped to a field.
SELECT_SQL = """
SELECT subject, source, revision, entry_date, updated_at, connection_taylor, connection_malea,
       resonant, word, sleep_hours, alcohol, caffeine, food, exercise,
       mood, stress, body, feelings, feeling_status
FROM home_dailies
ORDER BY entry_date
"""

_YES_NO = {True: "Yes", False: "No"}
_ALCOHOL = {"0": "0", "1-2": "1-2 drinks", "3-4": "3-4 drinks", "5+": "5+ drinks"}
_CAFFEINE = {
    "0": "0", "1": "1 cup", "2": "2 cups", "3+": "3+ cups",
    "1-2": "1-2 cups", "3-4": "3-4 cups", "5+": "5+ cups",
}
_FOOD = {
    "binge": "Binge-snacked", "unhealthy": "Ate unhealthily",
    "healthy": "Ate healthily", "very_healthy": "Ate especially healthily",
}
_EXERCISE = {"none": "None", "low": "Low", "mid": "Mid", "high": "High"}
_FEELING_STATUS = {"not_sure": "Not sure", "none": "None"}


def _number(value) -> Optional[str]:
    if isinstance(value, Decimal) and value == value.to_integral_value():
        value = int(value)
    return None if value is None else str(value)


def _text(value) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


# The one mapping table: database column -> (Form question title, frontmatter
# key, value -> Form option string). Values outside a table are dropped.
FIELDS = (
    ("resonant", "What is the most emotionally resonant thing that happened today?", "resonant_moment", _text),
    ("connection_taylor", "Meaningful connection with Taylor last 24h?", "connection_taylor", _YES_NO.get),
    ("connection_malea", "Meaningful connection with Malea last 24h?", "connection_malea", _YES_NO.get),
    ("word", "One word for the day", "one_word", _text),
    ("exercise", "Exercise today", "exercise", _EXERCISE.get),
    ("food", "Ate for fuel or comfort, last 24h", "eating", _FOOD.get),
    ("sleep_hours", "Sleep yesterday", "sleep", _number),
    ("mood", "Mood today", "mood", _number),
    ("stress", "Stress today", "stress", _number),
    ("body", "Body today", "body", _number),
    ("caffeine", "Caffeine today", "caffeine", _CAFFEINE.get),
    ("alcohol", "Alcohol yesterday", "alcohol", _ALCOHOL.get),
)

FEELING_HEADER = "Feelings"
FEELINGS_LIST_HEADER = "All feelings"

# Frontmatter keys for the note writer. Feeling chain columns are titled
# "<Name> feelings" and auto-key to `<name>_feelings`, except the two
# branches whose established journal keys are singular.
FIELD_MAPPINGS = {
    **{header: key for _, header, key, _ in FIELDS},
    FEELING_HEADER: "feeling",
    FEELINGS_LIST_HEADER: "feelings",
    "Bad feelings": "bad_feeling",
    "Tired feelings": "tired_feeling",
}

SHEET_CONFIG = SheetConfig(
    sheet_id=SOURCE_ID,
    name="Daily Journal (Home database)",
    account="personal",
    range="",
    timestamp_column="",
    outputs={"journal_notes": {"enabled": True, "folder": JOURNAL_FOLDER}},
    field_mappings=FIELD_MAPPINGS,
)


def fetch_rows(dsn: str) -> list[dict]:
    """Run the one SELECT in a read-only transaction and return dict rows."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, connect_timeout=15, row_factory=dict_row) as conn:
        conn.read_only = True
        return conn.execute(SELECT_SQL).fetchall()


def is_home_authored(row: dict) -> bool:
    """Whether a row's content was written in Home: saved there (`source`
    `home`), or imported from the Form and edited in Home since (every save
    bumps `revision` past its initial 1). An unedited imported row is a
    lossy reconstruction of a sheet row whose note already exists, so the
    source leaves that note alone."""
    revision = row.get("revision")
    return row.get("source") == "home" or (isinstance(revision, int) and revision > 1)


def _valid_paths(feelings) -> list[list[str]]:
    if not isinstance(feelings, list):
        return []
    return [
        [name.strip() for name in path]
        for path in feelings
        if isinstance(path, list) and 1 <= len(path) <= 3
        and all(isinstance(name, str) and name.strip() for name in path)
    ]


def row_to_entry(row: dict) -> Optional[JournalEntry]:
    """Map one `home_dailies` row to a JournalEntry, or None if unusable.

    The note date is `entry_date`, the New York calendar day the entry is
    for, never the time it was saved or edited.
    """
    raw_date = row.get("entry_date")
    try:
        entry_date = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date))
    except ValueError:
        return None
    if isinstance(entry_date, datetime):
        entry_date = entry_date.date()
    date_str = entry_date.isoformat()
    updated_at = row.get("updated_at")
    updated_str = updated_at.isoformat() if isinstance(updated_at, datetime) else str(updated_at)

    fields: dict = {}
    for column, header, _key, to_form in FIELDS:
        value = row.get(column)
        form_value = to_form(value) if value is not None else None
        if form_value is not None:
            fields[header] = form_value

    status = row.get("feeling_status")
    paths = _valid_paths(row.get("feelings")) if status == "chosen" else []
    if paths:
        primary = paths[0]
        fields[FEELING_HEADER] = primary[0]
        for parent, child in zip(primary, primary[1:]):
            fields[f"{parent} feelings"] = child
        fields[FEELINGS_LIST_HEADER] = [" > ".join(path) for path in paths]
    elif status in _FEELING_STATUS:
        fields[FEELING_HEADER] = _FEELING_STATUS[status]

    if not fields:
        return None
    row_hash = hashlib.sha256(f"{date_str}|{updated_str}".encode()).hexdigest()[:16]
    return JournalEntry(
        row_hash=row_hash,
        timestamp=updated_at if isinstance(updated_at, datetime) else datetime.combine(entry_date, datetime.min.time()),
        date_str=date_str,
        fields=fields,
        # Stored in synced_rows.raw_data, whose "<Branch> feelings" keys the
        # trend views read as wheel branches; the list is not a branch.
        raw_row={k: v for k, v in fields.items() if k != FEELINGS_LIST_HEADER},
    )
