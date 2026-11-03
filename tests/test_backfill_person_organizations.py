"""scripts/backfill_person_organizations.py: dry run, backup, idempotence, and how profiles are read."""
import hashlib
import json
import sqlite3
import stat
from pathlib import Path

import pytest

from api.services.person_entity import PersonEntity, PersonEntityStore
from scripts.backfill_person_organizations import backfill, main

pytestmark = pytest.mark.unit

JOBS = [
    {"company": "Current Org", "start_year": 2023, "end_year": None},
    {"company": "Past Org", "start_year": 2019, "start_month": 3, "end_year": 2022, "end_month": 8},
]


class World:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.path = tmp_path / "crm.db"
        self.people = PersonEntityStore(db_path=str(self.path))

    def person(self, name="Pat Profile", company="Current Org", **fields):
        return self.people.add(PersonEntity(canonical_name=name, emails=[f"{name.split()[0].lower()}@example.com"], company=company, **fields))

    def profiles(self, *profiles):
        path = self.tmp_path / "profiles.json"
        path.write_text(json.dumps({"profiles": list(profiles)}))
        return str(path)

    def held(self, person):
        return [(e["name"], e["source"], e["since"], e["until"]) for e in self.people.get_by_id(person.id).past_organizations]

    def digest(self):
        return hashlib.sha256(Path(self.path).read_bytes()).hexdigest()


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


class TestRun:
    def test_a_dry_run_changes_no_byte_and_reads_only(self, world, monkeypatch):
        person = world.person()
        before = world.digest()
        opened = []
        real_connect = sqlite3.connect
        monkeypatch.setattr(sqlite3, "connect", lambda database, *a, **k: opened.append(str(database)) or real_connect(database, *a, **k))
        stats = backfill(str(world.path), world.profiles({"person_id": person.id, "experience": JOBS}), dry_run=True)
        assert world.digest() == before and stats["status"] == "dry_run" and stats["people_to_change"] == 1
        assert opened and all("mode=ro" in database for database in opened)
        assert not list(world.path.parent.glob("*.bak-*"))

    def test_a_dry_run_works_without_the_column(self, world):
        person = world.person()
        conn = sqlite3.connect(world.path)
        conn.execute("ALTER TABLE person_entities DROP COLUMN past_organizations")
        conn.commit()
        conn.close()
        assert backfill(str(world.path), world.profiles({"person_id": person.id, "experience": JOBS}), dry_run=True)["people_to_change"] == 1

    def test_execute_backs_up_then_writes_and_a_second_run_changes_nothing(self, world):
        person = world.person()
        profiles = world.profiles({"person_id": person.id, "experience": JOBS})
        stats = backfill(str(world.path), profiles, dry_run=False)
        assert stats["people_written"] == 1 and stat.S_IMODE(Path(stats["backup"]).stat().st_mode) == 0o600
        assert world.held(person) == [("Past Org", "linkedin_profile", "2019-03", "2022-08")]
        again = backfill(str(world.path), profiles, dry_run=False)
        assert again["people_to_change"] == 0 and again["people_written"] == 0
        assert (world.people.get_by_id(person.id).company, world.people.get_by_id(person.id).position) == ("Current Org", None)

    def test_the_output_is_counts_only(self, world, capsys):
        person = world.person()
        main(["--db", str(world.path), "--profiles", world.profiles({"person_id": person.id, "experience": JOBS})])
        out = capsys.readouterr().out
        assert "people_to_change=1" in out and "Past Org" not in out and "Pat" not in out

    def test_execute_needs_the_deploy_confirmation(self, world, capsys):
        person = world.person()
        profiles = world.profiles({"person_id": person.id, "experience": JOBS})
        before = world.digest()
        with pytest.raises(SystemExit) as exit_info:
            main(["--db", str(world.path), "--profiles", profiles, "--execute"])
        assert exit_info.value.code == 2 and "--code-deployed" in capsys.readouterr().err and world.digest() == before
        assert main(["--db", str(world.path), "--profiles", profiles, "--execute", "--code-deployed"]) == 0
        assert world.held(person)


class TestReadingProfiles:
    def test_profiles_for_one_person_and_a_merged_id_are_combined(self, world):
        survivor = world.person()
        gone = world.person("Pat Two", None, hidden=True, hidden_reason=f"merged_into:{survivor.id}")
        job = lambda name: [{"company": name, "start_year": 2010, "end_year": 2012}]  # noqa: E731
        profiles = world.profiles(
            {"person_id": survivor.id, "experience": job("First Org")}, {"person_id": gone.id, "experience": job("Second Org")},
            {"person_id": survivor.id, "experience": []}, {"person_id": survivor.id, "experience": None})
        backfill(str(world.path), profiles, dry_run=False)
        assert sorted(name for name, *_ in world.held(survivor)) == ["First Org", "Second Org"]

    def test_the_current_company_and_open_jobs_are_not_past(self, world):
        person = world.person(company="Gamma")
        jobs = [{"company": "Gamma", "start_year": 2018, "end_year": 2021}, {"company": "Open Org", "start_year": 2022},
                {"company": "Open Org", "start_year": 2015, "end_year": 2022}, {"company": "Other Org", "start_year": 2010, "end_year": 2015}]
        backfill(str(world.path), world.profiles({"person_id": person.id, "experience": jobs}), dry_run=False)
        assert [name for name, *_ in world.held(person)] == ["Other Org"]

    def test_a_person_with_a_hand_entered_organization_is_left_alone(self, world):
        person = world.person(past_organizations=[{"name": "Hand Org", "source": "manual", "since": None, "until": None}])
        stats = backfill(str(world.path), world.profiles({"person_id": person.id, "experience": JOBS}), dry_run=False)
        assert world.held(person) == [("Hand Org", "manual", None, None)] and stats["people_with_manual_entries_skipped"] == 1

    def test_a_profile_that_lost_its_jobs_clears_the_entries_it_made(self, world):
        person = world.person()
        backfill(str(world.path), world.profiles({"person_id": person.id, "experience": JOBS}), dry_run=False)
        backfill(str(world.path), world.profiles({"person_id": person.id, "experience": JOBS[:1]}), dry_run=False)
        assert world.held(person) == []

    def test_unknown_hidden_and_owner_profiles_are_counted_not_written(self, world):
        hidden = world.person("Pat Hidden", hidden=True)
        owner = world.person("Pat Owner", category="self")
        profiles = world.profiles(*({"person_id": pid, "experience": JOBS} for pid in ("no-such-person", hidden.id, owner.id)))
        stats = backfill(str(world.path), profiles, dry_run=False)
        assert stats["profiles_unmatched"] == 3 and stats["people_to_change"] == 0

    @pytest.mark.parametrize("content", ["{not json", "[]", '{"profiles": "x"}', ""])
    def test_an_unreadable_file_changes_nothing(self, world, content):
        world.person()
        path = world.tmp_path / "bad.json"
        path.write_text(content)
        assert backfill(str(world.path), str(path), dry_run=True)["people_to_change"] == 0
        assert backfill(str(world.path), str(world.tmp_path / "missing.json"), dry_run=True)["people_to_change"] == 0
