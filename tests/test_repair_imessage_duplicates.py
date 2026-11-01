"""scripts/repair_imessage_duplicates.py removes stale duplicate iMessage
interactions conservatively, against synthetic temp databases."""
import hashlib
import logging
import sqlite3
from pathlib import Path

import pytest

from api.services.imessage_identity import make_key, parse_timestamp, source_id_for
from scripts.repair_imessage_duplicates import main, repair
from tests.imessage_identity_support import ImessageEnv, ts

pytestmark = pytest.mark.unit

PERSON_NAME = "Quillfeather Synthetic"
HANDLE = "+15557770199"
TEXT = "xylophone zebra synthetic body"


@pytest.fixture
def env(tmp_path, monkeypatch):
    environment = ImessageEnv(tmp_path, monkeypatch)
    environment.add_person("p1", name=PERSON_NAME)
    return environment


def run(env, dry_run=True):
    return repair(env.interactions_db, env.imessage_db, dry_run=dry_run,
                  person_store=env.people)


def file_hash(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def seed_renumbered_duplicate(env, *, valid_id="imessage_500", stale_id="imessage_100",
                              stale_person="p1", second=10):
    """One message (rowid 500) with a correct-id interaction and a leftover
    interaction under a rowid the store does not have."""
    env.add_message(500, ts(second), text=TEXT, handle=HANDLE)
    env.add_interaction(valid_id, ts(second), person="p1", title=f"← {TEXT}")
    env.add_interaction(stale_id, ts(second), person=stale_person, title=f"← {TEXT}")


class TestDeletion:
    def test_deletes_stale_duplicate_and_keeps_the_valid_row(self, env):
        seed_renumbered_duplicate(env)

        stats = run(env, dry_run=False)

        assert stats["deleted"] == 1
        assert [r[0] for r in env.interactions()] == ["imessage_500"]

    def test_valid_legacy_row_survives_when_its_rowid_sorts_before_the_stale_one(self, env):
        env.add_message(100, ts(10), text=TEXT)
        env.add_interaction("imessage_100", ts(10), person="p1", title=f"← {TEXT}")
        env.add_interaction("imessage_900", ts(10), person="p1", title=f"← {TEXT}")

        stats = run(env, dry_run=False)

        assert stats["deleted"] == 1
        assert [r[0] for r in env.interactions()] == ["imessage_100"]

    def test_stale_row_pointing_at_a_different_message_is_deleted(self, env):
        """Store row 100 exists but is another message, so id 100 is stale."""
        seed_renumbered_duplicate(env)
        # The row under imessage_100 is at second 10; store row 100 is at second 40.
        env.add_message(100, ts(40), text="unrelated")
        env.add_interaction("imessage_700", ts(40), person="p1")
        stats = run(env, dry_run=False)

        assert stats["deleted"] == 1
        assert "imessage_100" not in [r[0] for r in env.interactions()]

    def test_non_legacy_ids_are_never_stale(self, env):
        """Two rows, one message, but neither id is a legacy rowid id: kept."""
        key = make_key(parse_timestamp(ts(10)), "in")
        env.add_message(500, ts(10))
        env.add_interaction("imessage_500", ts(10), person="p1")
        env.add_interaction(source_id_for(key, 0), ts(10), person="p1")

        stats = run(env, dry_run=False)

        assert stats["stale_rows"] == 0
        assert stats["deleted"] == 0

    def test_keeps_stale_row_when_the_persons_differ(self, env):
        env.add_person("p2")
        seed_renumbered_duplicate(env, stale_person="p2")

        stats = run(env, dry_run=False)

        assert stats["deleted"] == 0
        assert stats["kept_person_mismatch"] == 1
        assert len(env.interactions()) == 2

    def test_keeps_rows_whose_key_has_no_store_message(self, env):
        env.add_interaction("imessage_100", ts(10), person="p1")
        env.add_interaction("imessage_101", ts(10), person="p1")

        stats = run(env, dry_run=False)

        assert stats["deleted"] == 0
        assert stats["kept_orphan"] == 2

    def test_deletes_at_most_the_number_of_extras_per_key(self, env):
        """Three rows for two messages: exactly one extra, though two rows are
        stale and share a person with a retained row."""
        env.add_message(500, ts(10), text=TEXT)
        env.add_message(501, ts(10, ".500000"), text=TEXT)
        env.add_interaction("imessage_500", ts(10), person="p1")
        env.add_interaction("imessage_101", ts(10), person="p1")
        env.add_interaction("imessage_102", ts(10), person="p1")

        stats = run(env, dry_run=False)

        assert stats["deleted"] == 1
        assert stats["kept_over_cap"] == 1
        assert len(env.interactions()) == 2

    def test_never_deletes_the_last_row_at_a_key(self, env):
        """Both rows stale, same person, one message: one must remain."""
        env.add_message(500, ts(10))
        env.add_interaction("imessage_101", ts(10), person="p1")
        env.add_interaction("imessage_102", ts(10), person="p1")

        stats = run(env, dry_run=False)

        assert stats["deleted"] == 1
        assert len(env.interactions()) == 1

    def test_no_extras_means_no_deletes(self, env):
        env.add_message(500, ts(10))
        env.add_message(501, ts(10, ".500000"))
        env.add_interaction("imessage_101", ts(10), person="p1")  # stale, but needed
        env.add_interaction("imessage_102", ts(10), person="p1")

        assert run(env, dry_run=False)["deleted"] == 0

    def test_unlinked_store_messages_count_toward_the_key(self, env):
        """The store has two messages at the key (one unlinked), so the two
        rows are not extras even though one id is stale."""
        env.add_message(500, ts(10), person="p1")
        env.add_message(501, ts(10, ".500000"), person=None)
        env.add_interaction("imessage_500", ts(10), person="p1")
        env.add_interaction("imessage_101", ts(10), person="p1")

        assert run(env, dry_run=False)["deleted"] == 0

    def test_hidden_peoples_store_messages_count_toward_the_key(self, env):
        env.add_person("hidden-p", hidden=True)
        env.add_message(500, ts(10), person="p1")
        env.add_message(501, ts(10, ".500000"), person="hidden-p")
        env.add_interaction("imessage_500", ts(10), person="p1")
        env.add_interaction("imessage_101", ts(10), person="p1")

        assert run(env, dry_run=False)["deleted"] == 0

    def test_other_sources_are_untouched(self, env):
        seed_renumbered_duplicate(env)
        env.add_interaction("imessage_100", ts(10), person="p1", source_type="gmail",
                            interaction_id="gmail-lookalike")

        run(env, dry_run=False)

        assert len(env.interactions("source_type = 'gmail'")) == 1


class TestIdempotence:
    def test_second_execute_deletes_nothing(self, env):
        seed_renumbered_duplicate(env)

        assert run(env, dry_run=False)["deleted"] == 1
        again = run(env, dry_run=False)

        assert again["deleted"] == 0
        assert again["would_delete"] == 0

    def test_repair_then_sync_leaves_one_row_per_message(self, env, monkeypatch):
        from tests.test_sync_imessage_interactions_identity import run_sync

        seed_renumbered_duplicate(env)
        run(env, dry_run=False)

        assert run_sync(env, monkeypatch)["inserted"] == 0
        assert env.interaction_count() == 1


class TestDryRun:
    def test_dry_run_writes_nothing(self, env):
        seed_renumbered_duplicate(env)
        before_hash = file_hash(env.interactions_db)
        before_rows = env.interactions()
        siblings = sorted(p.name for p in Path(env.interactions_db).parent.iterdir())

        stats = run(env, dry_run=True)

        assert stats["would_delete"] == 1
        assert stats["deleted"] == 0
        assert stats["backup"] is None
        assert env.interactions() == before_rows
        assert file_hash(env.interactions_db) == before_hash
        assert sorted(p.name for p in Path(env.interactions_db).parent.iterdir()) == siblings

    def test_dry_run_is_the_default_for_the_command_line(self, env):
        seed_renumbered_duplicate(env)

        main(["--interactions-db", env.interactions_db, "--imessage-db", env.imessage_db,
              "--people-db", str(env.tmp_path / "people.db")])

        assert len(env.interactions()) == 2

    def test_execute_flag_applies_changes(self, env):
        seed_renumbered_duplicate(env)

        main(["--execute", "--interactions-db", env.interactions_db,
              "--imessage-db", env.imessage_db,
              "--people-db", str(env.tmp_path / "people.db")])

        assert len(env.interactions()) == 1


class TestBackup:
    def test_execute_writes_a_restorable_timestamped_backup_first(self, env):
        seed_renumbered_duplicate(env)

        stats = run(env, dry_run=False)

        backup = Path(stats["backup"])
        assert backup.parent == Path(env.interactions_db).parent
        assert ".bak-" in backup.name
        conn = sqlite3.connect(str(backup))
        try:
            assert conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 2
        finally:
            conn.close()


class TestWouldBeInserted:
    def test_counts_linked_visible_messages_without_an_interaction(self, env):
        env.add_person("hidden-p", hidden=True)
        env.add_message(1, ts(10), person="p1")                # new
        env.add_message(2, ts(20), person="p1")                # has an interaction
        env.add_interaction("imessage_2", ts(20), person="p1")
        env.add_message(3, ts(30), person="hidden-p")          # hidden: never inserted
        env.add_message(4, ts(40), person=None)                # unlinked: never inserted

        assert run(env)["would_be_inserted"] == 1

    def test_reflects_the_deletes_of_the_same_run(self, env):
        """A message whose only matching interaction is the one deleted would
        be re-inserted by the sync; here the valid row stays, so none."""
        seed_renumbered_duplicate(env)

        assert run(env)["would_be_inserted"] == 0


class TestOutput:
    def test_output_is_aggregate_counts_only(self, env, caplog):
        seed_renumbered_duplicate(env)

        with caplog.at_level(logging.INFO):
            run(env, dry_run=False)
            run(env, dry_run=True)

        # The backup line names a path; everything else is counts.
        output = "\n".join(
            message for message in caplog.messages if "Backup written" not in message
        )
        assert "Rows scanned" in output
        for secret in (PERSON_NAME, HANDLE, TEXT, "p1", "imessage_"):
            assert secret not in output
