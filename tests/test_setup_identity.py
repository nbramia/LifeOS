"""
Tests for scripts/setup_identity.py's write/merge logic (#763).

Only the pure filesystem write/merge functions are tested here -- the
interactive search-and-prompt flow in main() is a thin wrapper around them
and isn't exercised (no server, no stdin). Every test operates on files
under tmp_path; nothing here ever touches a real .env or config/*.json.
"""
import json
from datetime import datetime, timezone

import pytest

from scripts.setup_identity import (
    _existing_partner_person_id,
    apply_identity_config,
    backup_file,
    merge_family_config,
    merge_relationship_overrides,
    write_env_updates,
)

pytestmark = pytest.mark.unit


class _FrozenClock:
    """Stand-in for the `datetime` name backup_file() uses, whose now()
    always returns the same instant -- simulates two script runs landing
    in the same microsecond."""
    def __init__(self, fixed_now):
        self._fixed_now = fixed_now

    def now(self, tz=None):
        return self._fixed_now


# ============================================================================
# backup_file
# ============================================================================

class TestBackupFile:
    def test_returns_none_when_file_absent(self, tmp_path):
        assert backup_file(tmp_path / "missing.json") is None

    def test_backs_up_existing_file_unchanged(self, tmp_path):
        target = tmp_path / "thing.json"
        target.write_text('{"a": 1}')
        backup = backup_file(target)
        assert backup is not None
        assert backup.exists()
        assert backup.read_text() == '{"a": 1}'
        assert backup != target
        # Original is untouched by the backup call itself.
        assert target.read_text() == '{"a": 1}'

    def test_backup_preserves_source_file_permissions(self, tmp_path):
        """A chmod 600 .env (holding a secret) must not get a
        group/world-readable backup copy."""
        target = tmp_path / ".env"
        target.write_text("LIFEOS_ANTHROPIC_API_KEY=sk-real-key\n")
        target.chmod(0o600)
        backup = backup_file(target)
        assert (backup.stat().st_mode & 0o777) == 0o600

    def test_same_second_backups_do_not_collide(self, tmp_path, monkeypatch):
        """Two backups taken in the same wall-clock second (same
        microsecond, in a mocked clock) must not overwrite each other --
        the second call gets a distinct name instead of clobbering the
        first backup."""
        import scripts.setup_identity as si

        target = tmp_path / "thing.json"
        target.write_text("version-1")
        fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        monkeypatch.setattr(si, "datetime", _FrozenClock(fixed_now))

        first_backup = backup_file(target)
        target.write_text("version-2")
        second_backup = backup_file(target)

        assert first_backup != second_backup
        assert first_backup.read_text() == "version-1"
        assert second_backup.read_text() == "version-2"


# ============================================================================
# write_env_updates
# ============================================================================

class TestWriteEnvUpdates:
    def test_noop_when_no_updates(self, tmp_path):
        env_path = tmp_path / ".env"
        example_path = tmp_path / ".env.example"
        example_path.write_text("LIFEOS_USER_NAME=\n")
        backup = write_env_updates(env_path, example_path, {})
        assert backup is None
        assert not env_path.exists()

    def test_creates_from_example_when_env_missing(self, tmp_path):
        env_path = tmp_path / ".env"
        example_path = tmp_path / ".env.example"
        example_path.write_text("LIFEOS_USER_NAME=\nLIFEOS_MY_PERSON_ID=\n")
        backup = write_env_updates(env_path, example_path, {"LIFEOS_MY_PERSON_ID": "abc-123"})
        assert backup is None  # nothing existed yet, so nothing to back up
        content = env_path.read_text()
        assert "LIFEOS_MY_PERSON_ID=abc-123" in content
        assert "LIFEOS_USER_NAME=" in content

    def test_merges_existing_file_preserves_unrelated_lines(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("LIFEOS_ANTHROPIC_API_KEY=sk-real-key\nLIFEOS_USER_NAME=Sam\n")
        example_path = tmp_path / ".env.example"
        write_env_updates(env_path, example_path, {"LIFEOS_MY_PERSON_ID": "abc-123"})
        content = env_path.read_text()
        assert "LIFEOS_ANTHROPIC_API_KEY=sk-real-key" in content
        assert "LIFEOS_USER_NAME=Sam" in content
        assert "LIFEOS_MY_PERSON_ID=abc-123" in content

    def test_updates_existing_key_in_place(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("LIFEOS_MY_PERSON_ID=old-id\nLIFEOS_USER_NAME=Sam\n")
        example_path = tmp_path / ".env.example"
        write_env_updates(env_path, example_path, {"LIFEOS_MY_PERSON_ID": "new-id"})
        lines = env_path.read_text().splitlines()
        assert lines.count("LIFEOS_MY_PERSON_ID=new-id") == 1
        assert not any("old-id" in line for line in lines)

    def test_uncomments_existing_commented_key(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("# LIFEOS_WORK_DOMAIN_2=othercompany.com\n")
        example_path = tmp_path / ".env.example"
        write_env_updates(env_path, example_path, {"LIFEOS_WORK_DOMAIN_2": "realcompany.com"})
        content = env_path.read_text()
        assert "LIFEOS_WORK_DOMAIN_2=realcompany.com" in content
        assert "# LIFEOS_WORK_DOMAIN_2=othercompany.com" not in content

    def test_quotes_values_with_whitespace(self, tmp_path):
        env_path = tmp_path / ".env"
        example_path = tmp_path / ".env.example"
        write_env_updates(env_path, example_path, {"LIFEOS_PARTNER_NAME": "Mary Ann"})
        assert 'LIFEOS_PARTNER_NAME="Mary Ann"' in env_path.read_text()

    def test_backs_up_existing_file_before_writing(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("LIFEOS_USER_NAME=Sam\n")
        example_path = tmp_path / ".env.example"
        backup = write_env_updates(env_path, example_path, {"LIFEOS_MY_PERSON_ID": "abc-123"})
        assert backup is not None
        assert backup.read_text() == "LIFEOS_USER_NAME=Sam\n"

    def test_duplicate_key_updates_the_line_dotenv_actually_honors(self, tmp_path):
        """A pre-existing, malformed .env with the same key assigned twice
        is a pre-existing anomaly this script didn't create -- but since
        dotenv parsers apply whichever assignment comes last, the script
        must update *that* line, or its write would have no effect on the
        value the app actually sees."""
        env_path = tmp_path / ".env"
        env_path.write_text("LIFEOS_WORK_DOMAIN=first.com\nLIFEOS_USER_NAME=Sam\nLIFEOS_WORK_DOMAIN=second.com\n")
        example_path = tmp_path / ".env.example"
        write_env_updates(env_path, example_path, {"LIFEOS_WORK_DOMAIN": "real.com"})
        lines = env_path.read_text().splitlines()
        assert lines[0] == "LIFEOS_WORK_DOMAIN=first.com"
        assert lines[2] == "LIFEOS_WORK_DOMAIN=real.com"

    def test_idempotent_run_twice_produces_same_result(self, tmp_path):
        env_path = tmp_path / ".env"
        example_path = tmp_path / ".env.example"
        updates = {"LIFEOS_MY_PERSON_ID": "abc-123", "LIFEOS_PARTNER_NAME": "Sam"}
        write_env_updates(env_path, example_path, updates)
        first = env_path.read_text()
        write_env_updates(env_path, example_path, updates)
        second = env_path.read_text()
        assert first == second
        assert second.count("LIFEOS_MY_PERSON_ID=") == 1
        assert second.count("LIFEOS_PARTNER_NAME=") == 1


# ============================================================================
# merge_family_config
# ============================================================================

class TestMergeFamilyConfig:
    def test_fresh_install_creates_from_example_shape_without_placeholders(self, tmp_path):
        config_path = tmp_path / "family_members.json"
        example_path = tmp_path / "family_members.example.json"
        example_path.write_text(json.dumps({
            "family_last_names": ["smith", "jones"],
            "family_exact_names": ["uncle bob"],
            "family_person_ids": ["uuid-of-partner"],
            "tracked_relationships": [{"name": "Parent Relationship", "person_ids": ["x"], "healthy_direction": "more"}],
            "default_selected_ids": ["uuid-1"],
        }))
        report = merge_family_config(config_path, example_path, family_last_names=["Ramirez"])
        config = json.loads(config_path.read_text())
        # The example's placeholder sample data must never be carried over.
        assert "smith" not in config["family_last_names"]
        assert "uuid-of-partner" not in config["family_person_ids"]
        assert config["tracked_relationships"] == []
        assert config["default_selected_ids"] == []
        # But the new value the operator gave us is there.
        assert config["family_last_names"] == ["Ramirez"]
        assert report["backup"] is None

    def test_fresh_install_without_example_starts_empty(self, tmp_path):
        config_path = tmp_path / "family_members.json"
        example_path = tmp_path / "family_members.example.json"  # doesn't exist
        merge_family_config(config_path, example_path, family_last_names=["Ramirez"])
        config = json.loads(config_path.read_text())
        assert config["family_last_names"] == ["Ramirez"]
        assert config["family_person_ids"] == []

    def test_merges_into_existing_file_preserves_other_keys(self, tmp_path):
        config_path = tmp_path / "family_members.json"
        config_path.write_text(json.dumps({
            "family_last_names": ["Ramirez"],
            "family_exact_names": ["grammy"],
            "family_person_ids": ["existing-id"],
            "tracked_relationships": [{"name": "Coparent", "person_ids": ["a", "b"], "healthy_direction": "more"}],
            "default_selected_ids": ["existing-id"],
        }))
        example_path = tmp_path / "family_members.example.json"
        report = merge_family_config(
            config_path, example_path,
            family_last_names=["Chen"],
            family_person_ids=["new-id"],
        )
        config = json.loads(config_path.read_text())
        assert set(config["family_last_names"]) == {"Ramirez", "Chen"}
        assert set(config["family_person_ids"]) == {"existing-id", "new-id"}
        # Untouched by this script.
        assert config["family_exact_names"] == ["grammy"]
        assert config["tracked_relationships"] == [{"name": "Coparent", "person_ids": ["a", "b"], "healthy_direction": "more"}]
        assert config["default_selected_ids"] == ["existing-id"]
        assert report["backup"] is not None

    def test_rerun_does_not_duplicate_entries(self, tmp_path):
        config_path = tmp_path / "family_members.json"
        example_path = tmp_path / "family_members.example.json"
        merge_family_config(config_path, example_path, family_last_names=["Ramirez"], family_person_ids=["id-1"])
        merge_family_config(config_path, example_path, family_last_names=["Ramirez"], family_person_ids=["id-1"])
        config = json.loads(config_path.read_text())
        assert config["family_last_names"] == ["Ramirez"]
        assert config["family_person_ids"] == ["id-1"]

    def test_dedupe_is_case_insensitive_for_surnames(self, tmp_path):
        config_path = tmp_path / "family_members.json"
        example_path = tmp_path / "family_members.example.json"
        config_path.write_text(json.dumps({"family_last_names": ["Ramirez"]}))
        merge_family_config(config_path, example_path, family_last_names=["ramirez", "Chen"])
        config = json.loads(config_path.read_text())
        assert config["family_last_names"] == ["Ramirez", "Chen"]


# ============================================================================
# merge_relationship_overrides
# ============================================================================

class TestMergeRelationshipOverrides:
    def test_fresh_install_creates_skeleton_without_placeholders(self, tmp_path):
        config_path = tmp_path / "relationship_overrides.json"
        example_path = tmp_path / "relationship_overrides.example.json"
        example_path.write_text(json.dumps({
            "strength_overrides": {"uuid-of-partner": 100.0},
            "circle_overrides": {"uuid-of-partner": 0},
            "partner_person_id": "",
        }))
        merge_relationship_overrides(config_path, example_path, partner_person_id="real-id")
        config = json.loads(config_path.read_text())
        assert config["strength_overrides"] == {}
        assert config["circle_overrides"] == {}
        assert config["partner_person_id"] == "real-id"

    def test_preserves_existing_strength_and_circle_overrides(self, tmp_path):
        config_path = tmp_path / "relationship_overrides.json"
        config_path.write_text(json.dumps({
            "strength_overrides": {"some-id": 80.0},
            "circle_overrides": {"some-id": 1},
            "partner_person_id": "",
        }))
        example_path = tmp_path / "relationship_overrides.example.json"
        report = merge_relationship_overrides(config_path, example_path, partner_person_id="real-id")
        config = json.loads(config_path.read_text())
        assert config["strength_overrides"] == {"some-id": 80.0}
        assert config["circle_overrides"] == {"some-id": 1}
        assert config["partner_person_id"] == "real-id"
        assert report["backup"] is not None

    def test_no_partner_id_leaves_existing_value_untouched(self, tmp_path):
        config_path = tmp_path / "relationship_overrides.json"
        config_path.write_text(json.dumps({
            "strength_overrides": {},
            "circle_overrides": {},
            "partner_person_id": "existing-partner-id",
        }))
        example_path = tmp_path / "relationship_overrides.example.json"
        merge_relationship_overrides(config_path, example_path, partner_person_id=None)
        config = json.loads(config_path.read_text())
        assert config["partner_person_id"] == "existing-partner-id"


# ============================================================================
# apply_identity_config -- the full write/merge pass
# ============================================================================

@pytest.fixture
def config_paths(tmp_path):
    return dict(
        env_path=tmp_path / ".env",
        env_example_path=tmp_path / ".env.example",
        family_config_path=tmp_path / "family_members.json",
        family_example_path=tmp_path / "family_members.example.json",
        relationship_config_path=tmp_path / "relationship_overrides.json",
        relationship_example_path=tmp_path / "relationship_overrides.example.json",
    )


class TestApplyIdentityConfig:
    def test_full_flow_writes_all_three_surfaces(self, config_paths):
        report = apply_identity_config(
            **config_paths,
            my_person_id="me-id",
            partner_name="Sam",
            partner_person_id="partner-id",
            family_last_names=["Ramirez"],
            family_person_ids=["sibling-id"],
            work_email_domain="acme.com",
            work_email_domain_2="othercompany.com",
            work_email_domains_extra=["thirdco.com"],
        )
        env_content = config_paths["env_path"].read_text()
        assert "LIFEOS_MY_PERSON_ID=me-id" in env_content
        assert "LIFEOS_PARTNER_NAME=Sam" in env_content
        assert "LIFEOS_WORK_DOMAIN=acme.com" in env_content
        assert "LIFEOS_WORK_DOMAIN_2=othercompany.com" in env_content
        assert "LIFEOS_WORK_DOMAINS_EXTRA=thirdco.com" in env_content

        family_config = json.loads(config_paths["family_config_path"].read_text())
        assert family_config["family_last_names"] == ["Ramirez"]
        assert family_config["family_person_ids"] == ["sibling-id"]

        relationship_config = json.loads(config_paths["relationship_config_path"].read_text())
        assert relationship_config["partner_person_id"] == "partner-id"

        assert report["family_report"] is not None
        assert report["relationship_report"] is not None

    def test_no_answers_writes_nothing(self, config_paths):
        report = apply_identity_config(**config_paths)
        assert report["env_updates"] == {}
        assert not config_paths["env_path"].exists()
        assert report["family_report"] is None
        assert not config_paths["family_config_path"].exists()
        assert report["relationship_report"] is None
        assert not config_paths["relationship_config_path"].exists()

    def test_unmatched_partner_still_sets_name_but_not_person_id(self, config_paths):
        """Mirrors the interactive no-match case: a partner name was given
        but couldn't be matched to an indexed person, so partner_person_id
        stays unset while LIFEOS_PARTNER_NAME is still recorded."""
        report = apply_identity_config(
            **config_paths,
            partner_name="Sam",
            partner_person_id=None,
        )
        env_content = config_paths["env_path"].read_text()
        assert "LIFEOS_PARTNER_NAME=Sam" in env_content
        assert report["relationship_report"] is None
        assert not config_paths["relationship_config_path"].exists()

    def test_only_work_domains_leaves_family_and_relationship_files_untouched(self, config_paths):
        apply_identity_config(**config_paths, work_email_domain="acme.com")
        assert not config_paths["family_config_path"].exists()
        assert not config_paths["relationship_config_path"].exists()


# ============================================================================
# Atomic writes -- a failure partway through must never leave a target file
# truncated or partially written.
# ============================================================================

class TestAtomicWrites:
    def test_env_write_failure_leaves_original_file_untouched(self, tmp_path, monkeypatch):
        import scripts.setup_identity as si

        env_path = tmp_path / ".env"
        env_path.write_text("LIFEOS_USER_NAME=Sam\n")
        example_path = tmp_path / ".env.example"

        monkeypatch.setattr(si.os, "replace", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            write_env_updates(env_path, example_path, {"LIFEOS_MY_PERSON_ID": "abc-123"})

        # The original file must be exactly as it was -- no truncation.
        assert env_path.read_text() == "LIFEOS_USER_NAME=Sam\n"
        # No leftover temp file.
        assert list(tmp_path.glob(".env.tmp-*")) == []

    def test_family_config_write_failure_leaves_original_file_untouched(self, tmp_path, monkeypatch):
        import scripts.setup_identity as si

        config_path = tmp_path / "family_members.json"
        original = json.dumps({"family_last_names": ["Ramirez"], "family_person_ids": []})
        config_path.write_text(original)
        example_path = tmp_path / "family_members.example.json"

        monkeypatch.setattr(si.os, "replace", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            merge_family_config(config_path, example_path, family_last_names=["Chen"])

        assert config_path.read_text() == original

    def test_env_write_preserves_existing_file_permissions(self, tmp_path):
        """.env can hold secrets (API keys) and may be chmod 600 -- the
        atomic replace must not silently widen that to the umask default."""
        env_path = tmp_path / ".env"
        env_path.write_text("LIFEOS_USER_NAME=Sam\n")
        env_path.chmod(0o600)
        example_path = tmp_path / ".env.example"
        write_env_updates(env_path, example_path, {"LIFEOS_MY_PERSON_ID": "abc-123"})
        assert (env_path.stat().st_mode & 0o777) == 0o600


class TestExistingPartnerPersonId:
    def test_missing_file_returns_empty(self, tmp_path):
        assert _existing_partner_person_id(tmp_path / "missing.json") == ""

    def test_reads_existing_value(self, tmp_path):
        config_path = tmp_path / "relationship_overrides.json"
        config_path.write_text(json.dumps({"partner_person_id": "old-id"}))
        assert _existing_partner_person_id(config_path) == "old-id"

    def test_malformed_json_returns_empty_instead_of_raising(self, tmp_path):
        config_path = tmp_path / "relationship_overrides.json"
        config_path.write_text("not valid json{")
        assert _existing_partner_person_id(config_path) == ""

    def test_non_dict_top_level_returns_empty_instead_of_raising(self, tmp_path):
        """A malformed config whose top level is a list (or any non-dict)
        must not crash a warning-only lookup."""
        config_path = tmp_path / "relationship_overrides.json"
        config_path.write_text(json.dumps(["not", "a", "dict"]))
        assert _existing_partner_person_id(config_path) == ""
