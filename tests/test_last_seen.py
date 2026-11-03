"""Tests for PersonEntity.last_seen: one definition, one writer, safe to read when NULL."""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from api.services.interaction_store import InteractionStore, get_last_real_interactions
from api.services.person_entity import PersonEntity, PersonEntityStore
from api.services.person_stats import refresh_person_stats, sync_last_seen

pytestmark = pytest.mark.unit

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _days_ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


class _Env:
    """A real person store and interactions table on disk."""

    def __init__(self, tmp_path: Path):
        self.crm_db = str(tmp_path / "crm.db")
        self.interaction_db = str(tmp_path / "interactions.db")
        self.store = PersonEntityStore(db_path=self.crm_db)
        conn = sqlite3.connect(self.crm_db)
        conn.execute(
            "CREATE TABLE source_entities (id TEXT PRIMARY KEY, canonical_person_id TEXT, observed_at TEXT)"
        )
        conn.commit()
        conn.close()
        InteractionStore(self.interaction_db)
        self._next_row = 0

    def person(self, person_id: str, stored: str | None = None) -> None:
        """Add a person, with `stored` as the value already in the column."""
        self.store.add(PersonEntity(id=person_id, canonical_name=f"Person {person_id}"))
        if stored is not None:
            self.force_stored(person_id, stored)

    def force_stored(self, person_id: str, value: str | None) -> None:
        conn = sqlite3.connect(self.crm_db)
        conn.execute("UPDATE person_entities SET last_seen = ? WHERE id = ?", (value, person_id))
        conn.commit()
        conn.close()

    def interaction(
        self,
        person_id: str,
        timestamp: str,
        source_type: str = "imessage",
        source_id: str | None = None,
        attendee_count: int | None = None,
    ) -> None:
        self._next_row += 1
        conn = sqlite3.connect(self.interaction_db)
        conn.execute(
            """INSERT INTO interactions
               (id, person_id, timestamp, source_type, title, source_id, attendee_count)
               VALUES (?, ?, ?, ?, 'synthetic', ?, ?)""",
            (f"row-{self._next_row}", person_id, timestamp, source_type,
             source_id or f"src-{self._next_row}", attendee_count),
        )
        conn.commit()
        conn.close()

    def group_sent(self, person_id: str, timestamp: str) -> None:
        """One recipient's row of a WhatsApp group message the owner sent."""
        self._next_row += 1
        self.interaction(
            person_id, timestamp, "whatsapp",
            source_id=f"whatsapp_msg{self._next_row}:{person_id}", attendee_count=4,
        )

    def stored(self, person_id: str) -> str | None:
        conn = sqlite3.connect(self.crm_db)
        row = conn.execute(
            "SELECT last_seen FROM person_entities WHERE id = ?", (person_id,)
        ).fetchone()
        conn.close()
        return row[0]


@pytest.fixture
def env(tmp_path):
    env = _Env(tmp_path)
    with patch("api.services.person_entity.get_person_entity_store", return_value=env.store), \
         patch("api.services.interaction_store.get_interaction_db_path", return_value=env.interaction_db), \
         patch.object(PersonEntityStore, "CRM_DB_PATH", Path(env.crm_db)):
        yield env


class TestDefinition:
    def test_is_the_latest_past_interaction(self, env):
        env.person("p")
        env.interaction("p", _days_ago(30))
        env.interaction("p", _days_ago(3), "gmail")
        env.interaction("p", _days_ago(10), "calendar")

        sync_last_seen()

        assert env.stored("p") == _days_ago(3)

    def test_future_rows_never_count(self, env):
        env.person("past-and-future")
        env.interaction("past-and-future", _days_ago(5))
        env.interaction("past-and-future", _days_ago(-30), "calendar")
        env.person("only-future")
        env.interaction("only-future", _days_ago(-30), "calendar")

        sync_last_seen()

        assert env.stored("past-and-future") == _days_ago(5)
        assert env.stored("only-future") is None

    def test_group_message_you_sent_never_counts(self, env):
        env.person("replied-long-ago")
        env.interaction("replied-long-ago", _days_ago(98), "whatsapp")
        env.group_sent("replied-long-ago", _days_ago(1))
        env.person("never-replied")
        env.group_sent("never-replied", _days_ago(1))

        sync_last_seen()

        assert env.stored("replied-long-ago") == _days_ago(98)
        assert env.stored("never-replied") is None

    def test_group_message_they_sent_counts(self, env):
        env.person("p")
        env.interaction("p", _days_ago(2), "whatsapp", source_id="whatsapp_msg9", attendee_count=4)

        sync_last_seen()

        assert env.stored("p") == _days_ago(2)

    def test_compares_instants_across_utc_offsets(self, env):
        """A row written with an offset is a later instant than its wall clock reads."""
        env.person("p")
        env.interaction("p", "2026-01-01T23:00:00-05:00")
        env.interaction("p", "2026-01-02T01:00:00+00:00")

        sync_last_seen()

        assert env.stored("p") == "2026-01-02T04:00:00+00:00"

    def test_source_entity_observations_never_count(self, env):
        """A contact sync re-stamping observed_at says nothing about the relationship."""
        env.person("no-interactions", stored=_days_ago(0))
        env.person("old-interaction")
        env.interaction("old-interaction", _days_ago(200))
        conn = sqlite3.connect(env.crm_db)
        conn.executemany(
            "INSERT INTO source_entities VALUES (?, ?, ?)",
            [("se1", "no-interactions", _days_ago(0)), ("se2", "old-interaction", _days_ago(0))],
        )
        conn.commit()
        conn.close()

        refresh_person_stats(None)

        assert env.stored("no-interactions") is None
        assert env.stored("old-interaction") == _days_ago(200)
        # first_seen keeps counting the earliest observation of any kind.
        assert env.store.get_by_id("no-interactions").first_seen is not None

    def test_score_and_last_seen_share_one_answer(self, env):
        env.person("p")
        env.interaction("p", _days_ago(40))
        env.group_sent("p", _days_ago(1))
        env.interaction("p", _days_ago(-5), "calendar")

        sync_last_seen()
        inputs = InteractionStore(env.interaction_db).get_strength_inputs("p", days_back=3650)

        assert env.store.get_by_id("p").last_seen == inputs["p"].last_interaction

    def test_helper_limits_to_requested_ids_past_the_sql_variable_limit(self, env):
        env.interaction("p1", _days_ago(1))
        env.interaction("p2", _days_ago(2))
        wanted = ["p1"] + [f"absent-{i}" for i in range(2000)]
        conn = sqlite3.connect(env.interaction_db)

        assert set(get_last_real_interactions(conn, wanted)) == {"p1"}
        assert set(get_last_real_interactions(conn)) == {"p1", "p2"}
        conn.close()


class TestClearing:
    def test_no_interactions_clears_a_stale_value(self, env):
        env.person("p", stored=_days_ago(1))

        result = sync_last_seen()

        assert env.stored("p") is None
        assert result["mismatched"] == 1
        assert result["cleared"] == 1

    def test_deleted_interactions_clear_on_targeted_refresh(self, env):
        env.person("p")
        env.interaction("p", _days_ago(1))
        refresh_person_stats(["p"])
        assert env.stored("p") == _days_ago(1)

        conn = sqlite3.connect(env.interaction_db)
        conn.execute("DELETE FROM interactions")
        conn.commit()
        conn.close()
        refresh_person_stats(["p"])

        assert env.stored("p") is None


class TestMerges:
    def test_merged_ids_rows_belong_to_the_survivor(self, env):
        env.person("survivor")
        env.person("merged-away", stored=_days_ago(0))
        env.store._merged_ids = {"merged-away": "survivor"}
        env.interaction("survivor", _days_ago(50))
        env.interaction("merged-away", _days_ago(7))

        sync_last_seen()

        assert env.stored("survivor") == _days_ago(7)
        assert env.stored("merged-away") is None

    def test_merge_chain_is_followed(self, env):
        env.person("survivor")
        env.store._merged_ids = {"oldest": "middle", "middle": "survivor"}
        env.interaction("oldest", _days_ago(7))

        sync_last_seen()

        assert env.stored("survivor") == _days_ago(7)

    def test_a_merged_id_row_older_than_the_survivors_does_not_win(self, env):
        env.person("survivor")
        env.store._merged_ids = {"merged-away": "survivor"}
        env.interaction("survivor", _days_ago(1))
        env.interaction("merged-away", _days_ago(400))

        sync_last_seen()

        assert env.stored("survivor") == _days_ago(1)

    @pytest.mark.parametrize("target", ["survivor", "merged-away"])
    def test_targeted_refresh_by_either_id_covers_both(self, env, target):
        env.person("survivor")
        env.person("merged-away", stored=_days_ago(0))
        env.store._merged_ids = {"merged-away": "survivor"}
        env.interaction("merged-away", _days_ago(7))

        sync_last_seen([target])

        assert env.stored("survivor") == _days_ago(7)
        assert env.stored("merged-away") is None


class TestScope:
    def test_targeted_run_leaves_other_people_alone(self, env):
        env.person("in-scope", stored=_days_ago(0))
        env.person("out-of-scope", stored=_days_ago(0))

        result = sync_last_seen(["in-scope"])

        assert env.stored("in-scope") is None
        assert env.stored("out-of-scope") == _days_ago(0)
        assert result["checked"] == 1

    def test_dry_run_reports_without_writing(self, env):
        env.person("p", stored=_days_ago(0))

        result = sync_last_seen(dry_run=True)

        assert result["mismatched"] == 1
        assert env.stored("p") == _days_ago(0)

    def test_second_run_finds_nothing(self, env):
        env.person("p")
        env.person("q", stored=_days_ago(0))
        env.interaction("p", _days_ago(3))

        assert sync_last_seen()["mismatched"] == 2
        assert sync_last_seen()["mismatched"] == 0

    def test_get_all_sees_the_written_value(self, env):
        env.person("p", stored=_days_ago(0))
        assert env.store.get_all()[0].last_seen is not None

        sync_last_seen()

        assert env.store.get_all()[0].last_seen is None


class TestStoreNeverWritesIt:
    def test_update_keeps_the_stored_value_whatever_the_object_holds(self, env):
        env.person("p")
        env.interaction("p", _days_ago(3))
        sync_last_seen()

        person = env.store.get_by_id("p")
        person.company = "Example Corp"
        person.last_seen = NOW
        env.store.update(person)

        assert env.stored("p") == _days_ago(3)
        assert env.store.get_by_id("p").company == "Example Corp"

    def test_a_stale_read_cannot_regress_a_later_write(self, env):
        env.person("p")
        env.interaction("p", _days_ago(30))
        sync_last_seen()
        read_before_the_new_interaction = env.store.get_by_id("p")

        env.interaction("p", _days_ago(1))
        sync_last_seen()
        env.store.update(read_before_the_new_interaction)

        assert env.stored("p") == _days_ago(1)

    def test_add_does_not_store_a_supplied_value(self, env):
        env.store.add(PersonEntity(id="p", canonical_name="Person P", last_seen=NOW))

        assert env.stored("p") is None

    def test_add_over_an_existing_row_keeps_the_stored_value(self, env):
        env.person("p", stored=_days_ago(3))

        env.store.add(PersonEntity(id="p", canonical_name="Person P", company="Example Corp"))

        assert env.stored("p") == _days_ago(3)
        assert env.store.get_by_id("p").company == "Example Corp"

    def test_refresh_of_counts_does_not_disturb_it(self, env):
        env.person("p")
        env.interaction("p", _days_ago(3))
        refresh_person_stats(None)

        person = env.store.get_by_id("p")
        assert person.message_count == 1
        assert env.stored("p") == _days_ago(3)

    def test_update_round_trips_every_column_and_never_last_seen(self, env):
        env.person("p", stored=_days_ago(3))
        conn = sqlite3.connect(env.crm_db)
        conn.row_factory = sqlite3.Row
        before = dict(conn.execute("SELECT * FROM person_entities WHERE id = 'p'").fetchone())
        conn.close()

        moment = datetime(2020, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
        updated = PersonEntity(
            id="p",
            canonical_name="Updated Name",
            display_name="Updated Display",
            emails=["updated@example.test"],
            company="Updated Co",
            position="Updated Role",
            past_organizations=[{"name": "Updated Past Org", "source": "manual", "since": None, "until": None}],
            linkedin_url="https://example.test/updated",
            category="work",
            vault_contexts=["Work/"],
            sources=["gmail"],
            first_seen=moment,
            last_seen=NOW,
            meeting_count=1,
            email_count=2,
            mention_count=3,
            message_count=4,
            slack_message_count=5,
            photo_count=6,
            related_notes=["note.md"],
            aliases=["Updated"],
            phone_numbers=["+12125550100"],
            phone_primary="+12125550100",
            confidence_score=0.5,
            tags=["tag"],
            notes="note",
            source_entity_count=7,
            birthday="01-02",
            hidden=True,
            hidden_at=moment,
            hidden_reason="reason",
            is_peripheral_contact=True,
            dunbar_circle=2,
        )
        updated.relationship_strength = 42.0

        env.store.update(updated)

        conn = sqlite3.connect(env.crm_db)
        conn.row_factory = sqlite3.Row
        after = dict(conn.execute("SELECT * FROM person_entities WHERE id = 'p'").fetchone())
        conn.close()
        written = dict(zip(PersonEntityStore._COLUMNS, env.store._entity_to_values(updated)))
        for column in PersonEntityStore._COLUMNS:
            assert after[column] == written[column], column
            if column != "id":
                assert after[column] != before[column], f"fixture leaves {column} unchanged"
        assert after["last_seen"] == _days_ago(3)
        assert env.store._entity_to_values(env.store.get_by_id("p")) == tuple(
            written[column] for column in PersonEntityStore._COLUMNS
        )

    def test_upsert_sql_covers_every_column_but_last_seen(self, env):
        conn = sqlite3.connect(env.crm_db)
        table = {row[1] for row in conn.execute("PRAGMA table_info(person_entities)")}
        conn.close()

        assert table - set(PersonEntityStore._COLUMNS) == {"last_seen"}
        assert len(env.store._entity_to_values(PersonEntity(canonical_name="x"))) == len(
            PersonEntityStore._COLUMNS
        )

    def test_merging_objects_does_not_invent_a_value(self):
        merged = PersonEntity(canonical_name="A", last_seen=NOW).merge(
            PersonEntity(canonical_name="A", last_seen=NOW)
        )
        assert merged.last_seen is None
