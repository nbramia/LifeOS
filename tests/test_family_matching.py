"""
Tests for family-name matching (#765): compound surnames, whitespace/case
normalization, and warning on configured names that match zero people.

Family matching has two parallel implementations that must stay in sync:
- api.routes.crm_models._utils.is_family_member
- api.services.person_entity._is_family_member

Both read module-level FAMILY_LAST_NAMES/FAMILY_EXACT_NAMES sets, which
existing callers (e.g. tests/test_create_contact_persons.py) monkeypatch
directly -- so these tests do the same rather than touching real config
files.
"""
import pytest

import api.routes.crm_models._utils as utils_mod
import api.services.person_entity as person_entity_mod
from api.services.person_entity import PersonEntity, PersonEntityStore

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# is_family_member (api/routes/crm_models/_utils.py)
# ---------------------------------------------------------------------------

class TestIsFamilyMemberUtils:
    def test_existing_single_word_surname_still_matches(self, monkeypatch):
        """Behavior that already worked must be unchanged."""
        monkeypatch.setattr(utils_mod, "FAMILY_LAST_NAMES", {"smith"})
        monkeypatch.setattr(utils_mod, "FAMILY_EXACT_NAMES", set())

        assert utils_mod.is_family_member("John Smith") is True
        assert utils_mod.is_family_member("Jane Doe") is False

    def test_existing_exact_name_still_matches(self, monkeypatch):
        monkeypatch.setattr(utils_mod, "FAMILY_LAST_NAMES", set())
        monkeypatch.setattr(utils_mod, "FAMILY_EXACT_NAMES", {"uncle bob"})

        assert utils_mod.is_family_member("Uncle Bob") is True
        assert utils_mod.is_family_member("uncle bob") is True

    def test_compound_surname_matches_regardless_of_internal_spacing(self, monkeypatch):
        """A compound configured surname must match a person whose full
        surname is that same compound name, not just its last word --
        and must match it whether the person's name has the internal
        space or not (e.g. contacts vs a messaging app)."""
        monkeypatch.setattr(utils_mod, "FAMILY_LAST_NAMES", {"van buren"})
        monkeypatch.setattr(utils_mod, "FAMILY_EXACT_NAMES", set())

        # Old behavior only ever checked the last whitespace-split token
        # ("buren"), so neither of these could ever match before this fix.
        assert utils_mod.is_family_member("Alice Van Buren") is True
        assert utils_mod.is_family_member("Alice VanBuren") is True

        # A name that only shares the trailing word must not match.
        assert utils_mod.is_family_member("Alice Buren") is False

    def test_compound_configured_surname_with_differing_case(self, monkeypatch):
        monkeypatch.setattr(utils_mod, "FAMILY_LAST_NAMES", {"Van Buren"})
        monkeypatch.setattr(utils_mod, "FAMILY_EXACT_NAMES", set())

        assert utils_mod.is_family_member("alice VANBUREN") is True

    def test_exact_name_matches_regardless_of_internal_spacing(self, monkeypatch):
        monkeypatch.setattr(utils_mod, "FAMILY_LAST_NAMES", set())
        monkeypatch.setattr(utils_mod, "FAMILY_EXACT_NAMES", {"mary jane"})

        assert utils_mod.is_family_member("MaryJane") is True
        assert utils_mod.is_family_member("Mary  Jane") is True

    def test_empty_and_none_name(self, monkeypatch):
        monkeypatch.setattr(utils_mod, "FAMILY_LAST_NAMES", {"smith"})
        monkeypatch.setattr(utils_mod, "FAMILY_EXACT_NAMES", set())

        assert utils_mod.is_family_member("") is False
        assert utils_mod.is_family_member("   ") is False


# ---------------------------------------------------------------------------
# _is_family_member (api/services/person_entity.py) -- must mirror the above
# ---------------------------------------------------------------------------

class TestIsFamilyMemberPersonEntity:
    def test_existing_single_word_surname_still_matches(self, monkeypatch):
        monkeypatch.setattr(person_entity_mod, "FAMILY_LAST_NAMES", {"smith"})
        monkeypatch.setattr(person_entity_mod, "FAMILY_EXACT_NAMES", set())

        assert person_entity_mod._is_family_member("John Smith") is True
        assert person_entity_mod._is_family_member("Jane Doe") is False

    def test_existing_exact_name_still_matches(self, monkeypatch):
        monkeypatch.setattr(person_entity_mod, "FAMILY_LAST_NAMES", set())
        monkeypatch.setattr(person_entity_mod, "FAMILY_EXACT_NAMES", {"uncle bob"})

        assert person_entity_mod._is_family_member("Uncle Bob") is True

    def test_compound_surname_matches_regardless_of_internal_spacing(self, monkeypatch):
        monkeypatch.setattr(person_entity_mod, "FAMILY_LAST_NAMES", {"van buren"})
        monkeypatch.setattr(person_entity_mod, "FAMILY_EXACT_NAMES", set())

        assert person_entity_mod._is_family_member("Alice Van Buren") is True
        assert person_entity_mod._is_family_member("Alice VanBuren") is True
        assert person_entity_mod._is_family_member("Alice Buren") is False

    def test_exact_name_matches_regardless_of_internal_spacing(self, monkeypatch):
        monkeypatch.setattr(person_entity_mod, "FAMILY_LAST_NAMES", set())
        monkeypatch.setattr(person_entity_mod, "FAMILY_EXACT_NAMES", {"mary jane"})

        assert person_entity_mod._is_family_member("MaryJane") is True
        assert person_entity_mod._is_family_member("Mary  Jane") is True


# ---------------------------------------------------------------------------
# _check_family_config_coverage -- warn on configured names matching no one
# ---------------------------------------------------------------------------

class TestCheckFamilyConfigCoverage:
    def _make_store(self, tmp_path, names):
        store = PersonEntityStore(str(tmp_path / "crm.db"))
        for name in names:
            store.add(PersonEntity(canonical_name=name))
        return store

    def test_warns_on_unmatched_configured_names(self, tmp_path, monkeypatch, caplog):
        store = self._make_store(tmp_path, ["Alice Smith"])
        monkeypatch.setattr(utils_mod, "get_person_entity_store", lambda: store)

        with caplog.at_level("WARNING"):
            utils_mod._check_family_config_coverage({"smith", "nomatchsurname"}, {"nomatchexact"})

        messages = [r.message for r in caplog.records]
        assert any("nomatchsurname" in m for m in messages)
        assert any("nomatchexact" in m for m in messages)
        assert not any("'smith'" in m for m in messages)

    def test_no_warning_when_all_configured_names_match(self, tmp_path, monkeypatch, caplog):
        store = self._make_store(tmp_path, ["Alice Van Buren"])
        monkeypatch.setattr(utils_mod, "get_person_entity_store", lambda: store)

        with caplog.at_level("WARNING"):
            utils_mod._check_family_config_coverage({"van buren"}, set())

        messages = [r.message for r in caplog.records]
        assert not any("matched zero indexed people" in m for m in messages)

    def test_no_op_when_no_family_names_configured(self, monkeypatch, caplog):
        """Must not touch the entity store at all when nothing is configured
        -- this keeps module import side-effect-free for fresh installs."""
        def _boom():
            raise AssertionError("should not query the entity store")

        monkeypatch.setattr(utils_mod, "get_person_entity_store", _boom)

        with caplog.at_level("WARNING"):
            utils_mod._check_family_config_coverage(set(), set())

        assert caplog.records == []

    def test_person_entity_module_coverage_check_mirrors_utils(self, tmp_path, monkeypatch, caplog):
        store = self._make_store(tmp_path, ["Alice Van Buren"])
        monkeypatch.setattr(person_entity_mod, "get_person_entity_store", lambda: store)

        with caplog.at_level("WARNING"):
            person_entity_mod._check_family_config_coverage({"van buren", "missingname"}, set())

        messages = [r.message for r in caplog.records]
        assert any("missingname" in m for m in messages)
        assert not any("'van buren'" in m for m in messages)
