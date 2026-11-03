"""Past organizations: names, the entry list, what is shown, and jobs read from a scraped profile."""
from datetime import date

import pytest

from api.services.person_organizations import (
    MAX_PAST_ORGANIZATIONS,
    SOURCE_MANUAL,
    SOURCE_PROFILE,
    clean_entry,
    derive_profile_organizations,
    format_past_organizations,
    is_organization_name,
    normalize_entries,
    org_key,
    same_org,
    visible_past_organizations,
)

pytestmark = pytest.mark.unit

TODAY = date(2026, 6, 1)


def _entry(name, source=SOURCE_PROFILE, since=None, until=None):
    return {"name": name, "source": source, "since": since, "until": until}


class TestNames:
    def test_case_punctuation_and_legal_suffix_are_ignored(self):
        assert org_key("Goldman Sachs & Co.") == org_key("goldman sachs") == org_key("The Goldman Sachs, Inc.")
        assert org_key("J.P. Morgan") == org_key("JP Morgan")

    @pytest.mark.parametrize("first,second", [("BlueLabs", "BlueLabs Analytics"), ("Example Corp.", "example")])
    def test_extended_forms_are_one_organization(self, first, second):
        assert same_org(first, second) and same_org(second, first)

    @pytest.mark.parametrize("first,second", [("Example Labs", "Example Foundation"), ("Blue", "Labs Blue"), ("", "Example"), (None, None)])
    def test_other_names_are_different_organizations(self, first, second):
        assert not same_org(first, second)

    @pytest.mark.parametrize("name", [None, "", "Self-employed", "Freelance", "N/A", "www.example.com", "https://example.com"])
    def test_placeholders_and_web_addresses_are_not_organizations(self, name):
        assert not is_organization_name(name)


class TestEntries:
    def test_a_valid_entry_is_cleaned(self):
        assert clean_entry({"name": "  Example   Labs ", "source": SOURCE_MANUAL, "since": "2019-03", "until": "2021"}) == _entry(
            "Example Labs", SOURCE_MANUAL, "2019-03", "2021")

    @pytest.mark.parametrize("source", ["message", "fact", "note", "email_domain", "linkedin", "", None])
    def test_an_entry_from_any_other_source_is_rejected(self, source):
        assert clean_entry({"name": "Message Corp", "source": source}) is None
        assert normalize_entries([{"name": "Message Corp", "source": source}]) == []

    @pytest.mark.parametrize("raw", [None, "Example", [], {}, {"source": SOURCE_MANUAL}, {"name": "Freelance", "source": SOURCE_MANUAL}])
    def test_malformed_entries_are_dropped(self, raw):
        assert clean_entry(raw) is None

    def test_a_bad_date_becomes_none(self):
        entry = clean_entry({"name": "Example", "source": SOURCE_MANUAL, "since": "last spring", "until": 2021})
        assert (entry["since"], entry["until"]) == (None, None)

    def test_one_organization_from_one_source_merges_dates(self):
        merged = normalize_entries([_entry("Example Inc", since="2018", until="2019"), _entry("example", since="2017", until="2018-12")])
        assert merged == [_entry("Example Inc", since="2017", until="2019")]

    def test_one_organization_from_two_sources_keeps_the_first(self):
        merged = normalize_entries([_entry("Example", SOURCE_MANUAL, "2020", "2021"), _entry("Example Labs", SOURCE_PROFILE, "2015", "2024")])
        assert merged == [_entry("Example", SOURCE_MANUAL, "2020", "2021")]

    def test_latest_first_and_capped(self):
        names = [e["name"] for e in normalize_entries([_entry("B Co"), _entry("A Co", until="2019"), _entry("C Co", until="2022-03")])]
        assert names == ["C Co", "A Co", "B Co"]
        many = [_entry(f"Org {i:02d}", until=f"20{i:02d}") for i in range(15)]
        kept = normalize_entries(many)
        assert len(kept) == MAX_PAST_ORGANIZATIONS == 10 and kept[0]["name"] == "Org 14"


class TestVisible:
    def test_the_current_company_is_never_shown_in_any_spelling(self):
        held = [_entry("Example Labs"), _entry("Acme Labs"), _entry("Other Org")]
        assert [e["name"] for e in visible_past_organizations(held, "example labs, Inc.")] == ["Acme Labs", "Other Org"]
        assert [e["name"] for e in visible_past_organizations(held, "Acme")] == ["Example Labs", "Other Org"]
        assert len(visible_past_organizations(held, None)) == 3

    def test_formatting_names_with_the_year_they_ended(self):
        held = [_entry("Example Labs", until="2021-05-04"), _entry("Undated Org"), _entry("Current Org", until="2020")]
        assert format_past_organizations(held, "Current Org") == "Example Labs (until 2021), Undated Org"
        assert format_past_organizations([], None) == ""
        assert format_past_organizations([_entry(f"Org {i}", until=str(2000 + i)) for i in range(6)], None, limit=2).count("Org") == 2


class TestProfileJobs:
    def _profile(self, *jobs):
        return {"experience": list(jobs)}

    def test_ended_jobs_are_past_and_open_ones_are_not(self):
        entries = derive_profile_organizations(self._profile(
            {"company": "Current Org", "start_year": 2023, "end_year": None},
            {"company": "Past Org", "start_year": 2019, "start_month": 3, "end_year": 2022, "end_month": 8},
        ), today=TODAY)
        assert entries == [_entry("Past Org", SOURCE_PROFILE, "2019-03", "2022-08")]

    def test_year_only_dates_stay_year_only_and_a_bad_month_leaves_the_year(self):
        entries = derive_profile_organizations(self._profile(
            {"company": "Past Org", "start_year": 2010, "start_month": 13, "end_year": 2012}), today=TODAY)
        assert (entries[0]["since"], entries[0]["until"]) == ("2010", "2012")

    @pytest.mark.parametrize("end_year,end_month,is_past", [(2099, None, False), (2027, None, False), (2026, 7, False), (2026, 6, True), (2026, None, True), (2025, 12, True)])
    def test_an_end_after_today_is_still_the_job(self, end_year, end_month, is_past):
        entries = derive_profile_organizations(self._profile(
            {"company": "Past Org", "start_year": 2020, "end_year": end_year, "end_month": end_month}), today=TODAY)
        assert bool(entries) is is_past

    def test_a_promotion_is_not_a_departure(self):
        entries = derive_profile_organizations(self._profile(
            {"company": "Gamma", "start_year": 2021},
            {"company": "Gamma Labs", "start_year": 2018, "end_year": 2021},
            {"company": "Other Org", "start_year": 2012, "end_year": 2018},
        ), current_company="", today=TODAY)
        assert [e["name"] for e in entries] == ["Other Org"]

    def test_the_current_company_is_left_out_even_for_an_ended_job(self):
        entries = derive_profile_organizations(self._profile(
            {"company": "Current Org Inc", "start_year": 2015, "end_year": 2018},
            {"company": "Other Org", "start_year": 2012, "end_year": 2015},
        ), current_company="current org", today=TODAY)
        assert [e["name"] for e in entries] == ["Other Org"]

    def test_several_jobs_at_one_organization_become_one_entry(self):
        entries = derive_profile_organizations(self._profile(
            {"company": "Past Org", "start_year": 2010, "end_year": 2012},
            {"company": "Past Org", "start_year": 2012, "end_year": 2015}), today=TODAY)
        assert len(entries) == 1 and (entries[0]["since"], entries[0]["until"]) == ("2010", "2015")

    @pytest.mark.parametrize("job", [{"company": ""}, {"company": None, "end_year": 2012}, {"company": "Freelance", "end_year": 2012},
                                     {"company": "Past Org", "end_year": "2012"}, {"company": "Past Org"}, "not a job", None])
    def test_unusable_jobs_are_skipped(self, job):
        assert derive_profile_organizations(self._profile(job), today=TODAY) == []
        assert derive_profile_organizations({}, today=TODAY) == [] == derive_profile_organizations({"experience": None}, today=TODAY)
