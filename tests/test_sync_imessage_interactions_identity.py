"""iMessage interactions are identified by (UTC second, direction), not rowid.

Covers scripts/sync_imessage_interactions.py against synthetic temp databases:
message matching across renumbered/reused rowids and sub-second drift, burst
handling, hidden and merged people, and honest counters.
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest

from api.services.imessage_identity import (
    claim_source_id,
    direction_from_title,
    interaction_key,
    make_key,
    message_key,
    parse_timestamp,
    source_id_for,
)
from api.services.source_entity import SourceEntityStore
from tests.imessage_identity_support import ImessageEnv, ts

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path, monkeypatch):
    return ImessageEnv(tmp_path, monkeypatch)


def run_sync(env, monkeypatch, dry_run=False, source_store=None):
    """Run the sync against the env's synthetic databases."""
    import scripts.sync_imessage_interactions as mod

    monkeypatch.setattr(mod.sys, "platform", "linux")
    monkeypatch.setattr(mod, "get_imessage_db_path", lambda: env.imessage_db)
    monkeypatch.setattr(mod, "get_interaction_db_path", lambda: env.interactions_db)
    monkeypatch.setattr(mod, "get_person_entity_store", lambda: env.people)
    monkeypatch.setattr(
        mod, "get_source_entity_store", lambda: source_store or _noop_source_store()
    )
    with patch("api.services.imessage.get_imessage_store", return_value=MagicMock()), \
         patch("api.services.imessage.join_imessages_to_entities",
               return_value={"messages_updated": 0}), \
         patch("api.services.person_stats.refresh_person_stats"):
        return mod.sync_imessage_interactions(dry_run=dry_run)


def _noop_source_store():
    store = MagicMock()
    store.add_or_update.return_value = (MagicMock(), True)
    return store


# ---------------------------------------------------------------------------
# Identity helper
# ---------------------------------------------------------------------------

class TestIdentityHelper:
    def test_key_floors_to_whole_second_and_reads_naive_as_utc(self):
        aware = message_key("2025-03-01T12:00:10.999999+00:00", 1)
        naive = message_key("2025-03-01T12:00:10.000001", 1)
        assert aware == naive
        assert message_key("2025-03-01T12:00:11.000001+00:00", 1) != aware

    def test_key_normalizes_offsets_to_utc(self):
        assert message_key("2025-03-01T07:00:10-05:00", 0) == message_key(ts(10), 0)

    def test_direction_distinguishes_keys(self):
        assert message_key(ts(10), 1) != message_key(ts(10), 0)

    def test_interaction_key_reads_direction_from_title_prefix(self):
        assert interaction_key(ts(10), "→ hi") == message_key(ts(10), 1)
        assert interaction_key(ts(10), "← hi") == message_key(ts(10), 0)
        assert direction_from_title("no arrow") is None
        assert interaction_key(ts(10), "no arrow") is None

    def test_unparseable_timestamp_has_no_key(self):
        assert message_key("not a time", 1) is None
        assert parse_timestamp(None) is None

    def test_claim_source_id_skips_taken_ordinals(self):
        key = make_key(parse_timestamp(ts(10)), "in")
        taken = {source_id_for(key, 0)}
        first = claim_source_id(key, taken)
        second = claim_source_id(key, taken)
        assert first == source_id_for(key, 1)
        assert second == source_id_for(key, 2)
        assert first.startswith("imessage_k_")


# ---------------------------------------------------------------------------
# Matching messages to interactions
# ---------------------------------------------------------------------------

class TestMessageMatching:
    def test_new_message_whose_rowid_is_taken_by_an_older_interaction_is_inserted(
        self, env, monkeypatch
    ):
        env.add_person("p1")
        # An older message's interaction still carries legacy id imessage_5.
        env.add_interaction("imessage_5", ts(1))
        # The store numbers that message 9 and has a different message at 5.
        env.add_message(9, ts(1), text="old message")
        env.add_message(5, ts(30), text="new message")

        stats = run_sync(env, monkeypatch)

        assert stats["inserted"] == 1
        assert stats["already_exists"] == 1
        rows = env.interactions()
        assert len(rows) == 2
        new_rows = [r for r in rows if r[0] != "imessage_5"]
        assert len(new_rows) == 1
        assert new_rows[0][0].startswith("imessage_k_")
        assert "new message" in new_rows[0][3]

    def test_renumbered_rowid_does_not_duplicate(self, env, monkeypatch):
        env.add_person("p1")
        env.add_message(5, ts(10))
        run_sync(env, monkeypatch)
        before = env.interactions()

        env.renumber_message(5, 77)
        stats = run_sync(env, monkeypatch)

        assert stats["inserted"] == 0
        assert stats["already_exists"] == 1
        assert env.interactions() == before

    def test_legacy_id_interaction_matches_message_with_new_rowid(self, env, monkeypatch):
        env.add_person("p1")
        env.add_interaction("imessage_5", ts(10))
        env.add_message(900, ts(10))

        stats = run_sync(env, monkeypatch)

        assert stats["inserted"] == 0
        assert env.interaction_count() == 1

    def test_sub_second_drift_does_not_duplicate(self, env, monkeypatch):
        env.add_person("p1")
        env.add_interaction("imessage_1", ts(10, ".100000"))
        env.add_message(1, ts(10, ".900000"))

        assert run_sync(env, monkeypatch)["inserted"] == 0
        assert env.interaction_count() == 1

    def test_naive_store_timestamp_is_read_as_utc(self, env, monkeypatch):
        env.add_person("p1")
        env.add_interaction("imessage_1", ts(10))
        env.add_message(1, "2025-03-01T12:00:10.250000")

        assert run_sync(env, monkeypatch)["inserted"] == 0

    def test_adjacent_seconds_are_distinct_messages(self, env, monkeypatch):
        """Floor, not round: .999 and the next second's .001 are two messages."""
        env.add_person("p1")
        env.add_interaction("imessage_1", ts(11, ".001000"))
        env.add_message(1, ts(10, ".999000"))

        assert run_sync(env, monkeypatch)["inserted"] == 1
        assert env.interaction_count() == 2

    def test_direction_is_part_of_the_key(self, env, monkeypatch):
        env.add_person("p1")
        env.add_interaction("imessage_1", ts(10), is_from_me=0)
        env.add_message(1, ts(10), is_from_me=1)

        assert run_sync(env, monkeypatch)["inserted"] == 1

    def test_same_second_burst_inserts_every_message_and_rerun_inserts_none(
        self, env, monkeypatch
    ):
        env.add_person("p1")
        for rowid in (1, 2, 3):
            env.add_message(rowid, ts(10, f".{rowid}00000"), text=f"burst {rowid}")

        first = run_sync(env, monkeypatch)
        assert first["inserted"] == 3
        ids = [r[0] for r in env.interactions()]
        assert len(set(ids)) == 3

        again = run_sync(env, monkeypatch)
        assert again["inserted"] == 0
        assert again["already_exists"] == 3
        assert env.interaction_count() == 3

    def test_burst_growing_later_inserts_only_the_new_message(self, env, monkeypatch):
        env.add_person("p1")
        env.add_message(1, ts(10, ".100000"))
        env.add_message(2, ts(10, ".200000"))
        run_sync(env, monkeypatch)

        env.add_message(3, ts(10, ".300000"))
        stats = run_sync(env, monkeypatch)

        assert stats["inserted"] == 1
        assert env.interaction_count() == 3

    def test_legacy_and_new_style_ids_coexist_in_one_key(self, env, monkeypatch):
        env.add_person("p1")
        key = make_key(parse_timestamp(ts(10)), "in")
        env.add_interaction("imessage_41", ts(10))
        # An id already taken at ordinal 0 by an earlier run.
        env.add_interaction(source_id_for(key, 0), ts(10))
        for rowid in (1, 2, 3):
            env.add_message(rowid, ts(10, f".{rowid}00000"))

        stats = run_sync(env, monkeypatch)

        assert stats["inserted"] == 1
        ids = [r[0] for r in env.interactions()]
        assert sorted(ids) == sorted(
            ["imessage_41", source_id_for(key, 0), source_id_for(key, 1)]
        )

    def test_dry_run_reports_would_insert_and_writes_nothing(self, env, monkeypatch):
        env.add_person("p1")
        env.add_message(1, ts(10))
        env.add_message(2, ts(20))

        stats = run_sync(env, monkeypatch, dry_run=True)

        assert stats["inserted"] == 2
        assert env.interaction_count() == 0

    def test_unparseable_timestamp_counts_as_error_not_insert(self, env, monkeypatch):
        env.add_person("p1")
        env.add_message(1, "not a timestamp")

        stats = run_sync(env, monkeypatch)

        assert stats["errors"] == 1
        assert stats["inserted"] == 0

    def test_message_for_missing_person_is_not_inserted(self, env, monkeypatch):
        env.add_message(1, ts(10), person="ghost")

        stats = run_sync(env, monkeypatch)

        assert stats["person_not_found"] == 1
        assert env.interaction_count() == 0


# ---------------------------------------------------------------------------
# Hidden and merged people
# ---------------------------------------------------------------------------

class TestHiddenAndMergedPeople:
    def test_hidden_person_gets_no_interaction_and_rerun_stays_empty(
        self, env, monkeypatch
    ):
        env.add_person("p1", hidden=True)
        env.add_message(1, ts(10))

        stats = run_sync(env, monkeypatch)
        assert stats["hidden_person"] == 1
        assert stats["inserted"] == 0
        assert env.interaction_count() == 0

        assert run_sync(env, monkeypatch)["inserted"] == 0

    @pytest.mark.parametrize("hidden_rowid, visible_rowid", [(1, 2), (2, 1)])
    def test_hidden_message_does_not_consume_a_visible_messages_interaction(
        self, env, monkeypatch, hidden_rowid, visible_rowid
    ):
        """Same second and direction, one hidden person's and one visible
        person's: the visible message must match the one existing interaction
        whichever order the store returns them in."""
        env.add_person("visible")
        env.add_person("hidden", hidden=True)
        env.add_interaction("imessage_700", ts(10), person="visible")
        env.add_message(hidden_rowid, ts(10), person="hidden")
        env.add_message(visible_rowid, ts(10), person="visible")

        stats = run_sync(env, monkeypatch)

        assert stats["inserted"] == 0
        assert env.interaction_count() == 1

    def test_merged_person_id_inserts_under_the_survivor(self, env, monkeypatch):
        env.add_person("survivor")
        env.merge("old-id", into_id="survivor")
        env.add_message(1, ts(10), person="old-id")

        stats = run_sync(env, monkeypatch)

        assert stats["inserted"] == 1
        rows = env.interactions()
        assert [r[1] for r in rows] == ["survivor"]

    def test_hidden_survivor_is_skipped(self, env, monkeypatch):
        env.add_person("survivor", hidden=True)
        env.merge("old-id", into_id="survivor")
        env.add_message(1, ts(10), person="old-id")

        stats = run_sync(env, monkeypatch)

        assert stats["hidden_person"] == 1
        assert env.interaction_count() == 0

    def test_hidden_person_handle_is_still_linked(self, env, monkeypatch):
        env.add_person("p1", hidden=True)
        env.add_message(1, ts(10), handle="+15550000009")
        store = SourceEntityStore(db_path=str(env.tmp_path / "crm.db"))

        with patch("api.services.person_entity.get_person_entity_store",
                   return_value=env.people):
            stats = run_sync(env, monkeypatch, source_store=store)

        assert stats["source_entities_created"] == 1
        linked = store.get_by_source("imessage", "+15550000009")
        assert linked is not None and linked.canonical_person_id == "p1"


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

class TestCounters:
    def test_inserted_equals_actual_row_growth(self, env, monkeypatch):
        env.add_person("p1")
        env.add_interaction("imessage_1", ts(5))
        env.add_message(1, ts(5))
        for rowid in range(2, 8):
            env.add_message(rowid, ts(10 + rowid))
        before = env.interaction_count()

        stats = run_sync(env, monkeypatch)

        assert stats["inserted"] == 6
        assert env.interaction_count() - before == stats["inserted"]

    def test_inserted_counts_rows_written_not_rows_attempted(self, env, monkeypatch):
        """Rows the database ignores must not be counted as inserted."""
        env.add_person("p1")
        for rowid in range(1, 4):
            env.add_message(rowid, ts(10 + rowid))
        # Every attempted row gets the same primary key; only one can land.
        monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=1))

        stats = run_sync(env, monkeypatch)

        assert env.interaction_count() == 1
        assert stats["inserted"] == 1

    def test_source_entities_created_counts_only_new_entities(self, env, monkeypatch):
        env.add_person("p1")
        env.add_message(1, ts(10), handle="+15550000007")
        env.add_message(2, ts(20), handle="+15550000007")
        store = SourceEntityStore(db_path=str(env.tmp_path / "crm.db"))

        with patch("api.services.person_entity.get_person_entity_store",
                   return_value=env.people):
            first = run_sync(env, monkeypatch, source_store=store)
            assert first["inserted"] == 2
            # Two messages, one handle: one entity.
            assert first["source_entities_created"] == 1

            env.add_message(3, ts(30), handle="+15550000007")
            env.add_message(4, ts(40), handle="+15550000008")
            second = run_sync(env, monkeypatch, source_store=store)

        assert second["inserted"] == 2
        # The known handle is only refreshed; the other handle is new.
        assert second["source_entities_created"] == 1
