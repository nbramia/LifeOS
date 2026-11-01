"""Synthetic fixtures shared by the iMessage interaction identity tests.

Builds an isolated message store, interactions database, and people store so
the sync and repair scripts run against temp data only.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from api.services.interaction_store import InteractionStore
from api.services.person_entity import PersonEntity, PersonEntityStore

BASE_TS = "2025-03-01T12:00:{sec:02d}{frac}+00:00"


def ts(sec: int, frac: str = "") -> str:
    """Synthetic UTC timestamp within one fixed minute."""
    return BASE_TS.format(sec=sec, frac=frac)


class ImessageEnv:
    """Temp message store + interactions db + people store."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.imessage_db = str(tmp_path / "imessage.db")
        self.interactions_db = str(tmp_path / "interactions.db")
        self.tmp_path = tmp_path

        conn = sqlite3.connect(self.imessage_db)
        conn.executescript(
            """
            CREATE TABLE messages (
                rowid INTEGER PRIMARY KEY,
                text TEXT,
                timestamp TEXT NOT NULL,
                is_from_me INTEGER NOT NULL,
                handle TEXT NOT NULL,
                handle_normalized TEXT,
                service TEXT NOT NULL,
                person_entity_id TEXT
            );
            """
        )
        conn.commit()
        conn.close()

        InteractionStore(db_path=self.interactions_db, strict=False)

        monkeypatch.setattr(
            PersonEntityStore, "MERGED_IDS_PATH", tmp_path / "merged_person_ids.json"
        )
        self.people = PersonEntityStore(str(tmp_path / "people.db"))
        self.people._blocklist.clear()
        self.people._merged_ids = {}

    # -- people -----------------------------------------------------------
    def add_person(self, person_id: str, hidden: bool = False, name: str = None) -> None:
        self.people.add(
            PersonEntity(
                id=person_id,
                canonical_name=name or f"Synthetic {person_id}",
                hidden=hidden,
            )
        )

    def merge(self, old_id: str, into_id: str) -> None:
        """Mark ``old_id`` merged into ``into_id`` (old row stays, hidden)."""
        self.add_person(old_id, hidden=True)
        self.people._merged_ids[old_id] = into_id

    # -- messages ---------------------------------------------------------
    def add_message(
        self,
        rowid: int,
        timestamp: str,
        is_from_me: int = 0,
        person: str = "p1",
        text: str = "synthetic text",
        handle: str = "+15550000001",
    ) -> None:
        conn = sqlite3.connect(self.imessage_db)
        conn.execute(
            "INSERT INTO messages (rowid, text, timestamp, is_from_me, handle, "
            "handle_normalized, service, person_entity_id) VALUES (?,?,?,?,?,?,?,?)",
            (rowid, text, timestamp, is_from_me, handle, handle, "iMessage", person),
        )
        conn.commit()
        conn.close()

    def delete_message(self, rowid: int) -> None:
        conn = sqlite3.connect(self.imessage_db)
        conn.execute("DELETE FROM messages WHERE rowid = ?", (rowid,))
        conn.commit()
        conn.close()

    def renumber_message(self, old: int, new: int) -> None:
        conn = sqlite3.connect(self.imessage_db)
        conn.execute("UPDATE messages SET rowid = ? WHERE rowid = ?", (new, old))
        conn.commit()
        conn.close()

    # -- interactions -----------------------------------------------------
    def add_interaction(
        self,
        source_id: str,
        timestamp: str,
        is_from_me: int = 0,
        person: str = "p1",
        title: str = None,
        interaction_id: str = None,
        source_type: str = "imessage",
    ) -> None:
        mark = "→" if is_from_me else "←"
        conn = sqlite3.connect(self.interactions_db)
        conn.execute(
            "INSERT INTO interactions (id, person_id, timestamp, source_type, title, "
            "snippet, source_link, source_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                interaction_id or f"i-{source_id}-{person}",
                person,
                timestamp,
                source_type,
                title if title is not None else f"{mark} synthetic text",
                None,
                "",
                source_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        conn.close()

    def interactions(self, where: str = "source_type = 'imessage'") -> list[tuple]:
        conn = sqlite3.connect(self.interactions_db)
        try:
            return conn.execute(
                f"SELECT source_id, person_id, timestamp, title, id FROM interactions "
                f"WHERE {where} ORDER BY source_id"
            ).fetchall()
        finally:
            conn.close()

    def interaction_count(self) -> int:
        return len(self.interactions("1=1"))
