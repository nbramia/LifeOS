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


def titled(env, source_id, text, person="p1", second=10, **kwargs):
    env.add_interaction(source_id, ts(second), person=person, title=f"← {text}", **kwargs)


def remaining(env):
    return sorted(r[0] for r in env.interactions())


class TestTitleGuard:
    def test_stale_row_with_different_text_is_kept(self, env):
        """The store has two messages in the second; one stale row carries text
        that no retained same-person row has, so it is the only copy."""
        env.add_message(500, ts(10), text="second")
        env.add_message(501, ts(10, ".500000"), text="first")
        titled(env, "imessage_100", "first")
        titled(env, "imessage_500", "second")
        key = make_key(parse_timestamp(ts(10)), "in")
        titled(env, source_id_for(key, 0), "second")

        stats = run(env, dry_run=False)

        assert stats["deleted"] == 0
        assert stats["kept_different_text"] == 1
        assert len(env.interactions()) == 3

    def test_true_duplicate_is_deleted_and_the_unique_stale_row_survives(self, env):
        env.add_message(500, ts(10), text="second")
        env.add_message(501, ts(10, ".500000"), text="first")
        titled(env, "imessage_100", "first")
        titled(env, "imessage_500", "second")
        titled(env, "imessage_900", "second")

        stats = run(env, dry_run=False)

        assert stats["deleted"] == 1
        assert remaining(env) == ["imessage_100", "imessage_500"]

    @pytest.mark.parametrize("stale, valid", [
        ("Hello,  World!", "hello world"),
        ("ＨＥＬＬＯ world", "hello World"),
        ("meet me at the cafe at 5", "meet me at the café at 5"),
    ])
    def test_equal_or_similar_text_is_deleted(self, env, stale, valid):
        env.add_message(500, ts(10), text=valid)
        titled(env, "imessage_500", valid)
        titled(env, "imessage_100", stale)

        assert run(env, dry_run=False)["deleted"] == 1
        assert remaining(env) == ["imessage_500"]

    def test_dissimilar_text_is_kept(self, env):
        env.add_message(500, ts(10), text="see you at noon")
        titled(env, "imessage_500", "see you at noon")
        titled(env, "imessage_100", "completely different words here")

        stats = run(env, dry_run=False)

        assert stats["deleted"] == 0
        assert stats["kept_different_text"] == 1

    def test_stale_row_without_text_is_deleted_against_any_same_person_row(self, env):
        env.add_message(500, ts(10), text="anything")
        titled(env, "imessage_500", "anything")
        titled(env, "imessage_100", "[No text content]")

        assert run(env, dry_run=False)["deleted"] == 1
        assert remaining(env) == ["imessage_500"]

    @pytest.mark.parametrize("stale_text", ["an actual sentence", "no text content"])
    def test_stale_row_with_text_is_kept_when_the_only_survivor_has_no_text(
        self, env, stale_text
    ):
        env.add_message(500, ts(10), text="")
        titled(env, "imessage_500", "[No text content]")
        titled(env, "imessage_100", stale_text)

        stats = run(env, dry_run=False)

        assert stats["deleted"] == 0
        assert stats["kept_different_text"] == 1

    def test_different_person_with_equal_text_is_still_a_mismatch(self, env):
        env.add_person("p2")
        env.add_message(500, ts(10), text="same words")
        titled(env, "imessage_500", "same words")
        titled(env, "imessage_100", "same words", person="p2")

        stats = run(env, dry_run=False)

        assert stats["deleted"] == 0
        assert stats["kept_person_mismatch"] == 1


class TestPartnerProtection:
    def test_two_similar_stale_rows_never_delete_each_other_to_nothing(self, env):
        env.add_message(500, ts(10), text="unrelated anchor message")
        titled(env, "imessage_500", "unrelated anchor message")
        titled(env, "imessage_100", "alpha beta gamma delta")
        titled(env, "imessage_200", "alpha beta gamma delta!")
        # extras = 2, so the cap alone would allow both deletions.

        stats = run(env, dry_run=False)

        assert stats["deleted"] == 1
        assert stats["kept_needed_as_partner"] == 1
        assert len({"imessage_100", "imessage_200"} & set(remaining(env))) == 1

    def test_a_partner_is_not_deleted_later_through_a_chain_of_similar_titles(self, env):
        from api.services.imessage_identity import title_match_tier

        words = "the weather today looks rather lovely outside so lets walk toward".split()
        a = "← " + " ".join(words[0:8])
        b = "← " + " ".join(words[1:9])
        c = "← " + " ".join(words[2:10])
        assert title_match_tier(a, b) is not None
        assert title_match_tier(b, c) is not None
        assert title_match_tier(a, c) is None

        env.add_message(500, ts(10), text="x")
        env.add_interaction("imessage_100", ts(10), person="p1", title=a)
        env.add_interaction("imessage_200", ts(10), person="p1", title=b)
        env.add_interaction("imessage_500", ts(10), person="p1", title=c)
        # extras = 2: a is deleted on the strength of b, so b must stay.

        stats = run(env, dry_run=False)

        assert stats["deleted"] == 1
        assert "imessage_200" in remaining(env)
        assert "imessage_100" not in remaining(env)


class TestPriorityOrder:
    def test_exact_duplicate_goes_before_a_similar_row_with_a_lower_rowid(self, env):
        env.add_message(500, ts(10), text="hello world")
        env.add_message(501, ts(10, ".500000"), text="other")
        titled(env, "imessage_500", "hello world")
        titled(env, "imessage_100", "hello world pals")  # similar, sorts first
        titled(env, "imessage_200", "hello world")       # exact duplicate
        # extras = 1: the cap must spend its one deletion on the exact copy.

        stats = run(env, dry_run=False)

        assert stats["deleted"] == 1
        assert remaining(env) == ["imessage_100", "imessage_500"]
        assert stats["kept_over_cap"] == 1


class TestTitleMatchTier:
    def test_tiers(self):
        from api.services.imessage_identity import title_match_tier

        assert title_match_tier("← hi there", "← Hi, there!") == 0
        assert title_match_tier("← hello world pals", "← hello world") == 1
        assert title_match_tier("← hello", "← completely other") is None
        assert title_match_tier("← [No text content]", "← anything") == 1
        assert title_match_tier("← [No text content]", "← [No text content]") == 0
        assert title_match_tier("← some words", "← [No text content]") is None

    def test_direction_must_match(self):
        from api.services.imessage_identity import title_match_tier

        assert title_match_tier("→ hi there", "← hi there") is None
        assert title_match_tier("→ [No text content]", "← anything") is None

    def test_punctuation_only_text_is_compared_as_written(self):
        from api.services.imessage_identity import title_match_tier

        assert title_match_tier("← !!!", "← ???") is None
        assert title_match_tier("← !!!", "← !!!") == 0


class TestWouldBeInsertedPersonAware:
    def test_matches_the_syncs_two_stage_logic(self, env):
        env.add_person("p2")
        env.add_message(1, ts(10), person="p1")
        env.add_message(2, ts(10, ".500000"), person="p2")
        env.add_interaction("imessage_1", ts(10), person="p1")

        # The sync would insert the second person's message.
        assert run(env)["would_be_inserted"] == 1
