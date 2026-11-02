"""Merging people never loses facts.

The primary's facts stay; the secondary's facts move to the primary. When both
hold a fact for the same (category, key) exactly one survives: a user-confirmed
fact beats an unconfirmed one, otherwise the primary's fact stays. Fact
associations follow the surviving facts.

These tests run the real SQL against temporary SQLite files created by the
production store classes, with synthetic people and facts only.
"""
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

PRIMARY = "primary-person"
SECONDARY = "secondary-person"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from api.services.interaction_store import Interaction, InteractionStore
    from api.services.person_entity import PersonEntity, PersonEntityStore
    from api.services.person_facts import PersonFactStore
    from api.services.relationship import RelationshipStore
    from api.services.source_entity import SourceEntityStore

    crm_db_path = str(tmp_path / "crm.db")
    interactions_db_path = str(tmp_path / "interactions.db")

    person_store = PersonEntityStore(crm_db_path)
    rel_store = RelationshipStore(crm_db_path)
    SourceEntityStore(crm_db_path)
    fact_store = PersonFactStore(crm_db_path)
    interaction_store = InteractionStore(interactions_db_path, strict=False)

    person_store.add(PersonEntity(id=PRIMARY, canonical_name="Primary Person"))
    person_store.add(PersonEntity(id=SECONDARY, canonical_name="Secondary Person"))
    interaction_store.add(Interaction(
        id="synthetic-1", person_id=SECONDARY,
        timestamp=datetime.now(timezone.utc), source_type="imessage",
        title="synthetic message",
    ))

    merged_ids = tmp_path / "merged_ids.json"
    merge_log = tmp_path / "merge_log.json"
    monkeypatch.setattr("scripts.merge_people.get_person_entity_store", lambda: person_store)
    monkeypatch.setattr("scripts.merge_people.get_crm_db_path", lambda: crm_db_path)
    monkeypatch.setattr("scripts.merge_people.get_interaction_db_path", lambda: interactions_db_path)
    monkeypatch.setattr("scripts.merge_people.MERGED_IDS_FILE", merged_ids)
    monkeypatch.setattr("scripts.merge_people.MERGE_LOG_FILE", merge_log)
    monkeypatch.setattr("api.services.relationship.get_relationship_store", lambda *a, **k: rel_store)
    monkeypatch.setattr("api.services.person_stats.refresh_person_stats", lambda *a, **k: {})
    monkeypatch.setattr("api.services.relationship_metrics.update_strength_for_person", lambda *a, **k: None)

    return {
        "crm": crm_db_path,
        "facts": fact_store,
        "people": person_store,
        "merge_log": merge_log,
    }


def _add(env, person_id, category, key, value, confirmed=False, fact_id=None):
    from api.services.person_facts import PersonFact

    fact = PersonFact(
        person_id=person_id, category=category, key=key, value=value,
        confirmed_by_user=confirmed,
    )
    if fact_id:
        fact.id = fact_id
    env["facts"].add(fact)
    return fact.id


def _owned(env, person_id):
    """{(category, key): (value, confirmed)} for facts a person owns."""
    return {
        (f.category, f.key): (f.value, f.confirmed_by_user)
        for f in env["facts"].get_for_person(person_id, include_shared=False)
    }


def _associations(env):
    conn = sqlite3.connect(env["crm"])
    try:
        return {
            (fact_id, person_id): bool(is_primary)
            for fact_id, person_id, is_primary in conn.execute(
                "SELECT fact_id, person_id, is_primary FROM person_fact_associations"
            )
        }
    finally:
        conn.close()


def _orphan_associations(env):
    conn = sqlite3.connect(env["crm"])
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM person_fact_associations "
            "WHERE fact_id NOT IN (SELECT id FROM person_facts)"
        ).fetchone()[0]
    finally:
        conn.close()


def _merge(dry_run=False):
    from scripts.merge_people import merge_people

    return merge_people(PRIMARY, SECONDARY, dry_run=dry_run)


class TestFactsSurviveMerge:
    def test_both_peoples_facts_end_up_on_the_primary(self, env):
        _add(env, PRIMARY, "family", "pet", "primary has a dog", confirmed=True)
        _add(env, PRIMARY, "work", "role", "primary role")
        _add(env, SECONDARY, "interests", "hobby", "secondary climbs")
        _add(env, SECONDARY, "travel", "trip", "secondary visited a lake", confirmed=True)

        stats = _merge()

        assert _owned(env, PRIMARY) == {
            ("family", "pet"): ("primary has a dog", True),
            ("work", "role"): ("primary role", False),
            ("interests", "hobby"): ("secondary climbs", False),
            ("travel", "trip"): ("secondary visited a lake", True),
        }
        assert _owned(env, SECONDARY) == {}
        assert stats["facts_moved"] == 2
        assert stats["facts_cleared"] == 0

    def test_primary_facts_are_untouched_row_for_row(self, env):
        kept_id = _add(env, PRIMARY, "family", "pet", "primary has a dog", confirmed=True)
        _add(env, SECONDARY, "work", "role", "secondary role")

        _merge()

        kept = env["facts"].get_by_id(kept_id)
        assert (kept.person_id, kept.value, kept.confirmed_by_user) == (PRIMARY, "primary has a dog", True)

    def test_secondary_with_no_facts_changes_nothing(self, env):
        _add(env, PRIMARY, "work", "role", "primary role")

        stats = _merge()

        assert _owned(env, PRIMARY) == {("work", "role"): ("primary role", False)}
        assert stats["facts_moved"] == 0
        assert stats["facts_cleared"] == 0

    def test_primary_with_no_facts_receives_the_secondarys(self, env):
        _add(env, SECONDARY, "work", "role", "secondary role", confirmed=True)

        _merge()

        assert _owned(env, PRIMARY) == {("work", "role"): ("secondary role", True)}


class TestCollisionRule:
    """Both people hold a fact for the same (category, key)."""

    def _clash(self, env, primary_confirmed, secondary_confirmed):
        primary_id = _add(env, PRIMARY, "work", "role", "primary value", confirmed=primary_confirmed)
        secondary_id = _add(env, SECONDARY, "work", "role", "secondary value", confirmed=secondary_confirmed)
        stats = _merge()
        return primary_id, secondary_id, stats

    def test_confirmed_secondary_beats_unconfirmed_primary(self, env):
        primary_id, secondary_id, stats = self._clash(env, False, True)

        assert _owned(env, PRIMARY) == {("work", "role"): ("secondary value", True)}
        assert env["facts"].get_by_id(primary_id) is None
        assert env["facts"].get_by_id(secondary_id).person_id == PRIMARY
        assert (stats["facts_moved"], stats["facts_cleared"]) == (1, 1)

    def test_confirmed_primary_beats_unconfirmed_secondary(self, env):
        primary_id, secondary_id, stats = self._clash(env, True, False)

        assert _owned(env, PRIMARY) == {("work", "role"): ("primary value", True)}
        assert env["facts"].get_by_id(primary_id) is not None
        assert env["facts"].get_by_id(secondary_id) is None
        assert (stats["facts_moved"], stats["facts_cleared"]) == (0, 1)

    def test_both_confirmed_keeps_the_primarys(self, env):
        _, _, stats = self._clash(env, True, True)

        assert _owned(env, PRIMARY) == {("work", "role"): ("primary value", True)}
        assert (stats["facts_moved"], stats["facts_cleared"]) == (0, 1)

    def test_neither_confirmed_keeps_the_primarys(self, env):
        _, _, stats = self._clash(env, False, False)

        assert _owned(env, PRIMARY) == {("work", "role"): ("primary value", False)}
        assert (stats["facts_moved"], stats["facts_cleared"]) == (0, 1)

    def test_null_confirmation_counts_as_unconfirmed(self, env):
        _add(env, PRIMARY, "work", "role", "primary value")
        secondary_id = _add(env, SECONDARY, "work", "role", "secondary value", confirmed=True)
        conn = sqlite3.connect(env["crm"])
        conn.execute("UPDATE person_facts SET confirmed_by_user = NULL WHERE person_id = ?", (PRIMARY,))
        conn.commit()
        conn.close()

        _merge()

        assert env["facts"].get_by_id(secondary_id).person_id == PRIMARY
        assert _owned(env, PRIMARY) == {("work", "role"): ("secondary value", True)}

    def test_collision_is_per_category_and_key(self, env):
        _add(env, PRIMARY, "work", "role", "primary role")
        _add(env, SECONDARY, "work", "employer", "secondary employer")
        _add(env, SECONDARY, "interests", "role", "secondary interest")

        stats = _merge()

        assert set(_owned(env, PRIMARY)) == {("work", "role"), ("work", "employer"), ("interests", "role")}
        assert (stats["facts_moved"], stats["facts_cleared"]) == (2, 0)


class TestFactAssociations:
    def test_secondary_associations_are_repointed_to_the_primary(self, env):
        owned_by_other = _add(env, "third-person", "family", "spouse", "a shared fact", fact_id="fact-other")
        moved = _add(env, SECONDARY, "work", "role", "secondary role", fact_id="fact-moved")
        env["facts"].add_association(owned_by_other, SECONDARY)
        env["facts"].add_association(moved, SECONDARY, is_primary=True)

        _merge()

        assert _associations(env) == {
            ("fact-other", PRIMARY): False,
            ("fact-moved", PRIMARY): True,
        }
        shared = {f.id for f in env["facts"].get_for_person(PRIMARY)}
        assert {"fact-other", "fact-moved"} <= shared

    def test_duplicate_association_is_dropped_and_is_primary_is_kept(self, env):
        _add(env, "third-person", "family", "spouse", "a shared fact", fact_id="fact-shared")
        env["facts"].add_association("fact-shared", PRIMARY, is_primary=False)
        env["facts"].add_association("fact-shared", SECONDARY, is_primary=True)

        _merge()

        assert _associations(env) == {("fact-shared", PRIMARY): True}

    def test_primary_flag_is_not_cleared_by_a_non_primary_duplicate(self, env):
        _add(env, "third-person", "family", "spouse", "a shared fact", fact_id="fact-shared")
        env["facts"].add_association("fact-shared", PRIMARY, is_primary=True)
        env["facts"].add_association("fact-shared", SECONDARY, is_primary=False)

        _merge()

        assert _associations(env) == {("fact-shared", PRIMARY): True}

    def test_duplicate_of_two_non_primary_associations_stays_non_primary(self, env):
        _add(env, "third-person", "family", "spouse", "a shared fact", fact_id="fact-shared")
        env["facts"].add_association("fact-shared", PRIMARY, is_primary=False)
        env["facts"].add_association("fact-shared", SECONDARY, is_primary=False)

        _merge()

        assert _associations(env) == {("fact-shared", PRIMARY): False}

    def test_losing_facts_leave_no_orphan_associations(self, env):
        # Secondary's fact loses to the primary's; primary's fact is replaced
        # by a confirmed secondary one.
        _add(env, PRIMARY, "work", "role", "primary role", confirmed=True, fact_id="p-keeps")
        _add(env, SECONDARY, "work", "role", "secondary role", fact_id="s-loses")
        _add(env, PRIMARY, "interests", "hobby", "primary hobby", fact_id="p-replaced")
        _add(env, SECONDARY, "interests", "hobby", "secondary hobby", confirmed=True, fact_id="s-wins")
        for fact_id in ("p-keeps", "s-loses", "p-replaced", "s-wins"):
            env["facts"].add_association(fact_id, "third-person")
        env["facts"].add_association("s-loses", SECONDARY, is_primary=True)

        _merge()

        assert _orphan_associations(env) == 0
        assert set(_associations(env)) == {("p-keeps", "third-person"), ("s-wins", "third-person")}

    def test_merge_works_when_the_associations_table_is_absent(self, env):
        _add(env, SECONDARY, "work", "role", "secondary role")
        conn = sqlite3.connect(env["crm"])
        conn.execute("DROP TABLE person_fact_associations")
        conn.commit()
        conn.close()

        stats = _merge()

        assert _owned(env, PRIMARY) == {("work", "role"): ("secondary role", False)}
        assert stats["facts_moved"] == 1


class TestDryRun:
    def _seed(self, env):
        _add(env, PRIMARY, "work", "role", "primary role", confirmed=True)
        _add(env, SECONDARY, "work", "role", "secondary role")
        _add(env, PRIMARY, "interests", "hobby", "primary hobby")
        _add(env, SECONDARY, "interests", "hobby", "secondary hobby", confirmed=True)
        _add(env, PRIMARY, "family", "pet", "primary pet")
        _add(env, SECONDARY, "travel", "trip", "secondary trip")
        _add(env, SECONDARY, "dates", "anniversary", "secondary date")

    def test_dry_run_counts_equal_what_execute_does(self, env):
        self._seed(env)

        dry = _merge(dry_run=True)
        executed = _merge(dry_run=False)

        assert (dry["facts_moved"], dry["facts_cleared"]) == (executed["facts_moved"], executed["facts_cleared"])
        assert (executed["facts_moved"], executed["facts_cleared"]) == (3, 2)
        assert len(_owned(env, PRIMARY)) == 5

    def test_dry_run_changes_no_facts(self, env):
        self._seed(env)
        before_primary, before_secondary = _owned(env, PRIMARY), _owned(env, SECONDARY)

        _merge(dry_run=True)

        assert _owned(env, PRIMARY) == before_primary
        assert _owned(env, SECONDARY) == before_secondary


class TestInterruptedMergeRecovery:
    def test_failed_crm_transaction_rolls_facts_back_and_recovery_finishes(self, env, monkeypatch):
        import scripts.merge_people as mp

        _add(env, PRIMARY, "work", "role", "primary role", confirmed=True)
        _add(env, SECONDARY, "work", "role", "secondary role")
        _add(env, SECONDARY, "travel", "trip", "secondary trip")
        before = (_owned(env, PRIMARY), _owned(env, SECONDARY))

        real_apply = mp._apply_fact_merge

        def apply_then_crash(*args, **kwargs):
            real_apply(*args, **kwargs)
            raise RuntimeError("simulated crash mid-transaction")

        monkeypatch.setattr(mp, "_apply_fact_merge", apply_then_crash)
        with pytest.raises(RuntimeError):
            _merge()

        assert (_owned(env, PRIMARY), _owned(env, SECONDARY)) == before
        assert env["merge_log"].exists()

        monkeypatch.setattr(mp, "_apply_fact_merge", real_apply)
        assert mp.recover_incomplete_merge() is True

        assert _owned(env, PRIMARY) == {
            ("work", "role"): ("primary role", True),
            ("travel", "trip"): ("secondary trip", False),
        }
        assert _owned(env, SECONDARY) == {}
        assert not env["merge_log"].exists()

    def test_recovery_after_the_crm_commit_keeps_the_moved_facts(self, env):
        import scripts.merge_people as mp

        _add(env, PRIMARY, "work", "role", "primary role")
        _add(env, SECONDARY, "travel", "trip", "secondary trip")
        _merge()
        env["merge_log"].write_text(json.dumps({
            "operation": "merge",
            "primary_id": PRIMARY,
            "secondary_id": SECONDARY,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "phase": "crm_done",
        }))

        assert mp.recover_incomplete_merge() is True

        assert set(_owned(env, PRIMARY)) == {("work", "role"), ("travel", "trip")}

    def test_re_running_the_merge_loses_nothing(self, env):
        _add(env, PRIMARY, "work", "role", "primary role")
        _add(env, SECONDARY, "travel", "trip", "secondary trip")

        first = _merge()
        second = _merge()

        assert set(_owned(env, PRIMARY)) == {("work", "role"), ("travel", "trip")}
        assert first["facts_moved"] == 1
        assert (second["facts_moved"], second["facts_cleared"]) == (0, 0)


class TestMergeRoute:
    def test_route_merge_keeps_facts_and_reports_both_counters(self, env, monkeypatch):
        from api.routes.crm import router as crm_router

        monkeypatch.setattr("api.routes.crm.get_person_entity_store", lambda: env["people"])
        monkeypatch.setattr("api.routes.crm.get_source_entity_store", lambda: object())
        _add(env, PRIMARY, "work", "role", "primary role", confirmed=True)
        _add(env, SECONDARY, "work", "role", "secondary role")
        _add(env, SECONDARY, "travel", "trip", "secondary trip")

        app = FastAPI()
        app.include_router(crm_router)
        with TestClient(app) as client:
            response = client.post(
                "/api/crm/people/merge",
                json={"primary_id": PRIMARY, "secondary_ids": [SECONDARY]},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["merged_ids"] == [SECONDARY]
        assert body["stats"]["facts_moved"] == 1
        assert body["stats"]["facts_cleared"] == 1
        assert _owned(env, PRIMARY) == {
            ("work", "role"): ("primary role", True),
            ("travel", "trip"): ("secondary trip", False),
        }
