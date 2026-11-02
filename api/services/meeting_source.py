"""
Meeting source type: the provider-neutral label for meeting notes.

Meeting records carry source type ``meeting`` plus a ``provider`` naming the
tool that recorded them (``wispr``, ``granola``, or NULL when the note names
neither). Rows still labelled ``granola`` are migrated by
``migrate_interactions_db`` and ``migrate_crm_db``, which the stores run on
initialization and ``scripts/migrate_meeting_source_type.py`` runs on demand.

Migration design:

- Each database migrates in one ``BEGIN IMMEDIATE`` transaction, so a failure
  rolls back to the prior state and concurrent initializers serialize: the
  loser re-reads inside the lock and finds nothing left to do.
- Note frontmatter is read before that transaction opens, each distinct note
  once, so the write lock is never held across file I/O.
- When the vault can't be trusted, the rename proceeds without a provider and
  the row ids go into ``meeting_provider_backfill``. Each later initialization
  retries until the vault is back, then fills them in and drops the table.
- Once complete, initialization costs one indexed existence check per row table
  and one ``sqlite_master`` lookup. Those checks also catch any ``granola`` row
  written later by a process that has not restarted onto this code.

Reads treat ``granola`` as ``meeting`` throughout (``normalize_source_type``,
``expand_source_types``, ``fold_legacy_counts``), so meeting data stays visible
if the migration has failed or has not reached a store yet.
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, Iterable, Optional

logger = logging.getLogger(__name__)

MEETING_SOURCE_TYPE = "meeting"
LEGACY_MEETING_SOURCE_TYPE = "granola"

PROVIDER_WISPR = "wispr"
PROVIDER_GRANOLA = "granola"

# Appended to the source_id of a pre-existing `meeting` row that shares its
# source_id with a migrated row but links elsewhere. Path checks skip these ids.
COLLISION_SUFFIX = "#dup-"

BACKFILL_TABLE = "meeting_provider_backfill"
# Exists only once person `sources` lists have been migrated.
PERSON_SOURCES_MARKER_TABLE = "meeting_person_sources_migrated"

# How long a migration or a provider-column ALTER waits on another writer.
BUSY_TIMEOUT_SECONDS = 30.0
# Enough to hold any real frontmatter block without reading a whole note, which
# can run to megabytes of inline images.
FRONTMATTER_READ_BYTES = 64 * 1024
# With this many distinct notes and none of them found, the vault is the likely
# problem (an empty mount, a wrong path), not the notes.
UNTRUSTED_VAULT_MIN_NOTES = 20

# Columns that hold a row's person link. Rows that agree on all of them are the
# same observation; rows that differ must both survive a collision.
_ROW_TABLE_LINK_COLUMNS = {
    "interactions": ("person_id",),
    "source_entities": ("canonical_person_id", "link_status"),
}

# A human's decision outranks an automatic link when two rows share a source_id:
# the indexer leaves a non-auto row alone but overwrites an auto one.
_LINK_STATUS_RANK = {"confirmed": 2, "rejected": 1}

# One failure alert per database per window, however many processes and restarts
# hit the failure. Claimed through a marker file beside the database, which works
# when the database itself is what is failing.
ALERT_WINDOW_SECONDS = 24 * 60 * 60
ALERT_MARKER_SUFFIX = ".meeting-migration-alert"
# Store init waits this long for the send, then moves on and leaves it running.
ALERT_SEND_WAIT_SECONDS = 10.0

_FRONTMATTER_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)[ \t]*:", re.MULTILINE)


def meeting_provider(frontmatter: Collection[str]) -> Optional[str]:
    """Provider named by a note's frontmatter keys, or None if it names neither."""
    if "wispr_id" in frontmatter:
        return PROVIDER_WISPR
    if "granola_id" in frontmatter:
        return PROVIDER_GRANOLA
    return None


def normalize_source_type(source_type: str) -> str:
    """Map the legacy meeting source type to ``meeting``; pass others through."""
    return MEETING_SOURCE_TYPE if source_type == LEGACY_MEETING_SOURCE_TYPE else source_type


def normalize_sources(sources: Iterable[str]) -> list[str]:
    """Normalize a person ``sources`` list, keeping order and dropping duplicates."""
    return list(dict.fromkeys(normalize_source_type(s) for s in sources))


def expand_source_types(source_types: Iterable[str]) -> list[str]:
    """Filter values to match in SQL: either meeting name matches rows under both."""
    expanded: list[str] = []
    for source_type in source_types:
        if normalize_source_type(source_type) == MEETING_SOURCE_TYPE:
            expanded.extend((MEETING_SOURCE_TYPE, LEGACY_MEETING_SOURCE_TYPE))
        else:
            expanded.append(source_type)
    return list(dict.fromkeys(expanded))


def fold_legacy_counts(counts: dict[str, int]) -> dict[str, int]:
    """Combine per-source-type counts so legacy rows count under ``meeting``."""
    folded: dict[str, int] = {}
    for source_type, count in counts.items():
        key = normalize_source_type(source_type)
        folded[key] = folded.get(key, 0) + count
    return folded


def ensure_provider_column(conn: sqlite3.Connection, table: str) -> None:
    """Add the provider column to a row table, tolerating a concurrent adder."""
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if "provider" in columns:
        return
    conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_SECONDS * 1000)}")
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN provider TEXT")
        logger.info("Added provider column to %s", table)
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc):
            raise


def note_path_from_source_id(source_id: Optional[str]) -> Optional[str]:
    """Note path inside a source_id: ``<path>`` or ``<path>:<person name>``, maybe suffixed."""
    if not source_id:
        return None
    source_id = source_id.partition(COLLISION_SUFFIX)[0]
    separator = source_id.rfind(".md:")
    return source_id[: separator + 3] if separator >= 0 else source_id


def _locate_note(path: str, vault_root: Path) -> Optional[Path]:
    """Find a note by its stored path, or under this vault if stored from another machine."""
    try:
        stored = Path(path)
        if stored.is_file():
            return stored
        parts = stored.parts
        for index, part in enumerate(parts):
            if part == vault_root.name:
                rerooted = vault_root.joinpath(*parts[index + 1:])
                if rerooted.is_file():
                    return rerooted
    except (OSError, ValueError):
        pass
    return None


def _frontmatter_keys(note: Path) -> set[str]:
    """Top-level frontmatter keys, read by line so malformed YAML still yields them."""
    try:
        with open(note, "rb") as handle:
            head = handle.read(FRONTMATTER_READ_BYTES)
    except OSError:
        return set()
    lines = head.decode("utf-8-sig", errors="replace").splitlines()
    if not lines or lines[0].strip() != "---":
        return set()
    block: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        block.append(line)
    return set(_FRONTMATTER_KEY.findall("\n".join(block)))


def _read_providers(paths: set[str], vault_root: Optional[Path]) -> Optional[dict[str, str]]:
    """
    Provider per note path, reading each distinct note once.

    A note that can't be found or names neither tool gets ``granola``. Returns
    None when the vault can't be trusted, so absence from disk would say nothing
    about the note.
    """
    try:
        if vault_root is None or not vault_root.is_dir() or not any(vault_root.iterdir()):
            return None
    except OSError:
        return None
    providers: dict[str, str] = {}
    found = 0
    for path in paths:
        note = _locate_note(path, vault_root)
        if note is None:
            providers[path] = PROVIDER_GRANOLA
            continue
        found += 1
        providers[path] = meeting_provider(_frontmatter_keys(note)) or PROVIDER_GRANOLA
    if found == 0 and len(paths) >= UNTRUSTED_VAULT_MIN_NOTES:
        return None
    return providers


@dataclass
class MigrationResult:
    """What one migration run did. Counts only: never paths or names."""

    renamed: int = 0
    collisions_merged: int = 0
    collisions_kept_apart: int = 0
    providers_filled: int = 0
    providers_pending: int = 0
    person_sources_updated: int = 0
    notes_read: int = 0
    write_seconds: float = 0.0
    did_work: bool = False


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _has_legacy_rows(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            f"SELECT 1 FROM {table} WHERE source_type = ? LIMIT 1",
            (LEGACY_MEETING_SOURCE_TYPE,),
        ).fetchone()
        is not None
    )


def _pending_note_paths(conn: sqlite3.Connection, table: str) -> set[str]:
    """Distinct note paths that the coming transaction needs a provider for."""
    source_ids = [
        row[0]
        for row in conn.execute(
            f"SELECT source_id FROM {table} WHERE source_type = ?",
            (LEGACY_MEETING_SOURCE_TYPE,),
        )
    ]
    if _table_exists(conn, BACKFILL_TABLE):
        source_ids += [
            row[0]
            for row in conn.execute(
                f"SELECT t.source_id FROM {BACKFILL_TABLE} b JOIN {table} t ON t.id = b.row_id"
            )
        ]
    return {path for path in map(note_path_from_source_id, source_ids) if path}


def _provider_for(source_id: Optional[str], providers: Optional[dict[str, str]]) -> Optional[str]:
    """Provider to store now, or None to defer the row to the backfill table."""
    if providers is None:
        return None
    path = note_path_from_source_id(source_id)
    if path is None:
        return PROVIDER_GRANOLA
    return providers.get(path)


def _link_rank(link: dict) -> int:
    return _LINK_STATUS_RANK.get(link.get("link_status"), 0)


def _resolve_collisions(
    conn: sqlite3.Connection, table: str, result: MigrationResult
) -> dict[str, str]:
    """
    Clear the way for legacy rows whose source_id already has a ``meeting`` row.

    When both rows hold the same link the ``meeting`` row is a duplicate and goes,
    handing over its provider (the returned map of legacy row id to provider). When
    they differ, both links must survive, so one row takes a suffixed source_id: the
    ``meeting`` row, unless it holds the stronger human decision, in which case the
    legacy row does and the decision stays on the source_id the indexer looks up.
    """
    links = _ROW_TABLE_LINK_COLUMNS[table]
    legacy_links = ", ".join(f"old.{column}" for column in links)
    meeting_links = ", ".join(f"new.{column}" for column in links)
    rows = conn.execute(
        f"""
        SELECT old.id, new.id, new.provider, {legacy_links}, {meeting_links}
        FROM {table} old
        JOIN {table} new ON new.source_type = ? AND new.source_id = old.source_id
        WHERE old.source_type = ?
        """,
        (MEETING_SOURCE_TYPE, LEGACY_MEETING_SOURCE_TYPE),
    ).fetchall()

    inherited: dict[str, str] = {}
    for legacy_id, meeting_id, provider, *link_values in rows:
        legacy_link = dict(zip(links, link_values[: len(links)]))
        meeting_link = dict(zip(links, link_values[len(links) :]))
        if legacy_link == meeting_link:
            conn.execute(f"DELETE FROM {table} WHERE id = ?", (meeting_id,))
            if provider:
                inherited[legacy_id] = provider
            result.collisions_merged += 1
        else:
            displaced = legacy_id if _link_rank(meeting_link) > _link_rank(legacy_link) else meeting_id
            conn.execute(
                f"UPDATE {table} SET source_id = source_id || ? || id WHERE id = ?",
                (COLLISION_SUFFIX, displaced),
            )
            result.collisions_kept_apart += 1
    return inherited


def _rename_rows(
    conn: sqlite3.Connection,
    table: str,
    providers: Optional[dict[str, str]],
    result: MigrationResult,
) -> None:
    inherited = _resolve_collisions(conn, table, result)
    rows = conn.execute(
        f"SELECT id, source_id FROM {table} WHERE source_type = ?",
        (LEGACY_MEETING_SOURCE_TYPE,),
    ).fetchall()

    updates = []
    deferred = []
    for row_id, source_id in rows:
        provider = inherited.get(row_id) or _provider_for(source_id, providers)
        updates.append((MEETING_SOURCE_TYPE, provider, row_id))
        if provider is None:
            deferred.append((row_id,))
    # A provider already on the row came from indexing the note, so it stands.
    conn.executemany(
        f"UPDATE {table} SET source_type = ?, provider = COALESCE(provider, ?) WHERE id = ?",
        updates,
    )
    result.renamed += len(updates)
    result.providers_filled += len(updates) - len(deferred)

    if deferred:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {BACKFILL_TABLE} (row_id TEXT PRIMARY KEY)")
        conn.executemany(f"INSERT OR IGNORE INTO {BACKFILL_TABLE} (row_id) VALUES (?)", deferred)


def _finish_backfill(
    conn: sqlite3.Connection,
    table: str,
    providers: Optional[dict[str, str]],
    result: MigrationResult,
) -> None:
    """Fill providers on deferred rows where they are known; drop the table when done."""
    if not _table_exists(conn, BACKFILL_TABLE):
        return
    if providers is not None:
        rows = conn.execute(
            f"""
            SELECT b.row_id, t.id, t.source_id
            FROM {BACKFILL_TABLE} b LEFT JOIN {table} t ON t.id = b.row_id
            """
        ).fetchall()
        for row_id, existing_id, source_id in rows:
            provider = _provider_for(source_id, providers)
            if existing_id is not None and provider is None:
                continue
            if existing_id is not None:
                conn.execute(
                    f"UPDATE {table} SET provider = ? WHERE id = ? AND provider IS NULL",
                    (provider, row_id),
                )
                result.providers_filled += 1
            conn.execute(f"DELETE FROM {BACKFILL_TABLE} WHERE row_id = ?", (row_id,))
    remaining = conn.execute(f"SELECT COUNT(*) FROM {BACKFILL_TABLE}").fetchone()[0]
    result.providers_pending = remaining
    if remaining == 0:
        conn.execute(f"DROP TABLE {BACKFILL_TABLE}")


def _migrate_person_sources(conn: sqlite3.Connection, result: MigrationResult) -> None:
    """
    Rewrite only the ``sources`` column, under the caller's write lock.

    Holding the lock across the read and the write serializes this against
    concurrent person updates, so neither overwrites the other. A stale entity
    written back can't restore the legacy name, because ``PersonEntityStore``
    normalizes ``sources`` as it serializes.
    """
    rows = conn.execute(
        "SELECT id, sources FROM person_entities WHERE sources LIKE ?",
        (f'%"{LEGACY_MEETING_SOURCE_TYPE}"%',),
    ).fetchall()
    for person_id, raw_sources in rows:
        try:
            sources = json.loads(raw_sources)
        except ValueError:
            logger.warning("Person %s has unparseable sources; left unchanged", person_id)
            continue
        if not isinstance(sources, list):
            continue
        conn.execute(
            "UPDATE person_entities SET sources = ? WHERE id = ?",
            (json.dumps(normalize_sources(sources)), person_id),
        )
        result.person_sources_updated += 1
    conn.execute(f"CREATE TABLE IF NOT EXISTS {PERSON_SOURCES_MARKER_TABLE} (completed_at TEXT)")
    conn.execute(
        f"INSERT INTO {PERSON_SOURCES_MARKER_TABLE} (completed_at) "
        "SELECT datetime('now') WHERE NOT EXISTS "
        f"(SELECT 1 FROM {PERSON_SOURCES_MARKER_TABLE})"
    )


def _log_pending(db_path: str, pending: int) -> None:
    logger.warning(
        "Meeting source migration on %s: %d rows await a provider because the vault is "
        "unavailable; the next store initialization retries",
        Path(db_path).name,
        pending,
    )


def _default_vault_root() -> Optional[Path]:
    from config.settings import settings

    return Path(settings.vault_path) if settings.vault_path else None


def _migrate(
    db_path: str,
    row_table: str,
    migrate_people: bool,
    vault_root: Optional[Path],
    busy_timeout: float,
) -> MigrationResult:
    result = MigrationResult()
    conn = sqlite3.connect(db_path, timeout=busy_timeout, isolation_level=None)
    try:
        has_rows = _table_exists(conn, row_table)
        has_people = migrate_people and _table_exists(conn, "person_entities")
        if has_rows:
            ensure_provider_column(conn, row_table)

        has_legacy = has_rows and _has_legacy_rows(conn, row_table)
        has_backfill = has_rows and _table_exists(conn, BACKFILL_TABLE)
        people_need_work = has_people and (
            has_legacy or not _table_exists(conn, PERSON_SOURCES_MARKER_TABLE)
        )
        if not has_legacy and not has_backfill and not people_need_work:
            return result

        providers = None
        if has_legacy or has_backfill:
            paths = _pending_note_paths(conn, row_table)
            if paths:
                root = vault_root if vault_root is not None else _default_vault_root()
                providers = _read_providers(paths, root)
                result.notes_read = len(paths) if providers is not None else 0
            else:
                providers = {}

        if providers is None and not has_legacy and not people_need_work:
            # Only deferred providers remain and the vault still can't supply them.
            result.providers_pending = conn.execute(
                f"SELECT COUNT(*) FROM {BACKFILL_TABLE}"
            ).fetchone()[0]
            _log_pending(db_path, result.providers_pending)
            return result

        started = time.monotonic()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if has_rows:
                _rename_rows(conn, row_table, providers, result)
                _finish_backfill(conn, row_table, providers, result)
            if has_people:
                _migrate_person_sources(conn, result)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        result.write_seconds = time.monotonic() - started
        result.did_work = True
    finally:
        conn.close()

    logger.info(
        "Meeting source migration on %s: renamed=%d merged=%d kept_apart=%d providers_filled=%d "
        "person_sources=%d notes_read=%d write_seconds=%.3f",
        Path(db_path).name,
        result.renamed,
        result.collisions_merged,
        result.collisions_kept_apart,
        result.providers_filled,
        result.person_sources_updated,
        result.notes_read,
        result.write_seconds,
    )
    if result.providers_pending:
        _log_pending(db_path, result.providers_pending)
    return result


def migrate_interactions_db(
    db_path: str,
    vault_root: Optional[Path] = None,
    busy_timeout: float = BUSY_TIMEOUT_SECONDS,
) -> MigrationResult:
    """Migrate an interactions database. Raises on failure, leaving it unchanged."""
    return _migrate(db_path, "interactions", False, vault_root, busy_timeout)


def migrate_crm_db(
    db_path: str,
    vault_root: Optional[Path] = None,
    busy_timeout: float = BUSY_TIMEOUT_SECONDS,
) -> MigrationResult:
    """Migrate source entities and person ``sources`` in one transaction on a CRM database."""
    return _migrate(db_path, "source_entities", True, vault_root, busy_timeout)


def _claim_alert(db_path: str) -> bool:
    """Take the alert window for this database. Only the claimant sends."""
    marker = f"{db_path}{ALERT_MARKER_SUFFIX}"
    try:
        if time.time() - os.stat(marker).st_mtime < ALERT_WINDOW_SECONDS:
            return False
        os.unlink(marker)
    except FileNotFoundError:
        pass
    try:
        os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return False
    return True


def _deliver_alert(db_path: str, error: BaseException) -> None:
    """Send the Telegram alert, giving the window back if it doesn't go out."""
    sent = False
    try:
        from api.services.telegram import send_message

        sent = send_message(
            f"MEETING SOURCE MIGRATION FAILED on {Path(db_path).name}: {type(error).__name__}. "
            "The database is unchanged and reads treat granola rows as meeting. The next store "
            "initialization retries. See the server log, and meeting_source_migration in "
            "/api/crm/data-health."
        )
    except Exception:
        logger.exception("Could not send the meeting source migration alert")
    if not sent:
        logger.warning("Meeting source migration alert was not sent")
        try:
            os.unlink(f"{db_path}{ALERT_MARKER_SUFFIX}")
        except OSError:
            pass


def _alert_migration_failure(db_path: str, error: BaseException) -> None:
    """Alert on a failed migration, debounced. Nothing here can reach the caller."""
    try:
        if _claim_alert(db_path):
            sender = threading.Thread(target=_deliver_alert, args=(db_path, error), daemon=True)
            sender.start()
            sender.join(ALERT_SEND_WAIT_SECONDS)
    except Exception:
        logger.exception("Could not raise the meeting source migration alert")


def run_at_store_init(migrate, db_path: str) -> None:
    """
    Run a migration from a store's init. A failure is logged and alerted, never raised.

    A provider deferred because the vault is unavailable is not a failure: it logs a
    warning and retries, with no alert, so a sync lag doesn't page anyone.
    """
    try:
        migrate(db_path)
    except Exception as error:
        logger.exception(
            "MEETING SOURCE MIGRATION FAILED on %s. The database is unchanged and the next "
            "store initialization retries. Reads treat granola rows as meeting until then.",
            Path(db_path).name,
        )
        _alert_migration_failure(db_path, error)


def _row_table_status(db_path: str, table: str) -> dict:
    """Unfolded counts for one row table, read directly from the database."""
    status: dict = {"state": "unavailable"}
    if not Path(db_path).is_file():
        return status
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if not _table_exists(conn, table):
            return status
        counts = dict(
            conn.execute(
                f"SELECT source_type, COUNT(*) FROM {table} WHERE source_type IN (?, ?) GROUP BY 1",
                (LEGACY_MEETING_SOURCE_TYPE, MEETING_SOURCE_TYPE),
            ).fetchall()
        )
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        null_provider = None
        if "provider" in columns:
            null_provider = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE source_type = ? AND provider IS NULL",
                (MEETING_SOURCE_TYPE,),
            ).fetchone()[0]
        pending = 0
        if _table_exists(conn, BACKFILL_TABLE):
            pending = conn.execute(f"SELECT COUNT(*) FROM {BACKFILL_TABLE}").fetchone()[0]
        legacy = counts.get(LEGACY_MEETING_SOURCE_TYPE, 0)
        status = {
            "state": "not_migrated" if legacy else "provider_deferred" if pending else "complete",
            "legacy_rows": legacy,
            "meeting_rows": counts.get(MEETING_SOURCE_TYPE, 0),
            "null_provider": null_provider,
            "provider_backfill_pending": pending,
        }
        if table == "source_entities" and _table_exists(conn, "person_entities"):
            status["person_sources_legacy"] = conn.execute(
                "SELECT COUNT(*) FROM person_entities WHERE sources LIKE ?",
                (f'%"{LEGACY_MEETING_SOURCE_TYPE}"%',),
            ).fetchone()[0]
        return status
    finally:
        conn.close()


def migration_status(interactions_db: str, crm_db: str) -> dict:
    """
    Raw migration state per store, the one view that doesn't treat granola as meeting.

    ``not_migrated`` means legacy rows remain, because a migration failed or hasn't
    reached the store. ``provider_deferred`` means rows await a provider from the vault.
    A null provider is also normal for a meeting note that names no recorder.
    """
    return {
        "interactions": _row_table_status(interactions_db, "interactions"),
        "source_entities": _row_table_status(crm_db, "source_entities"),
    }


def rollback_interactions_db(db_path: str) -> int:
    """Return an interactions database to the ``granola`` label. Returns rows changed."""
    return _rollback(db_path, "interactions", False)


def rollback_crm_db(db_path: str) -> int:
    """Return a CRM database to the ``granola`` label. Returns rows changed."""
    return _rollback(db_path, "source_entities", True)


def _rollback(db_path: str, table: str, people: bool) -> int:
    """
    Reverse the label in one transaction, leaving provider values in their column.
    A UNIQUE conflict with an existing ``granola`` row rolls this back and raises
    rather than choosing between the two rows.
    """
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            changed = conn.execute(
                f"UPDATE {table} SET source_type = ? WHERE source_type = ?",
                (LEGACY_MEETING_SOURCE_TYPE, MEETING_SOURCE_TYPE),
            ).rowcount
            conn.execute(f"DROP TABLE IF EXISTS {BACKFILL_TABLE}")
            if people:
                conn.execute(f"DROP TABLE IF EXISTS {PERSON_SOURCES_MARKER_TABLE}")
                rows = conn.execute(
                    "SELECT id, sources FROM person_entities WHERE sources LIKE ?",
                    (f'%"{MEETING_SOURCE_TYPE}"%',),
                ).fetchall()
                for person_id, raw_sources in rows:
                    restored = list(dict.fromkeys(
                        LEGACY_MEETING_SOURCE_TYPE if source == MEETING_SOURCE_TYPE else source
                        for source in json.loads(raw_sources)
                    ))
                    conn.execute(
                        "UPDATE person_entities SET sources = ? WHERE id = ?",
                        (json.dumps(restored), person_id),
                    )
            conn.execute("COMMIT")
            return changed
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
