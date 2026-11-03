"""Organization questions can be answered from the people-index profiles.

A deterministic stand-in for the embedding search: a person answers a question when their
profile's descriptive lines name the organization it asks about, using the synthetic questions in
scripts/retrieval_eval/people_organization_queries.txt.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.services.person_entity import PersonEntity
from api.services.person_indexer import _descriptive_lines, generate_person_document

pytestmark = pytest.mark.unit

QUERIES = Path(__file__).resolve().parent.parent / "scripts" / "retrieval_eval" / "people_organization_queries.txt"
ORGANIZATIONS = ["Example Co", "Sample Labs", "Placeholder Foundation", "Fictional Partners"]


def _summary():
    return SimpleNamespace(
        relationship_strength=50, total_interactions_90d=0, channels=[], active_channels=[], primary_channel=None,
        contact_on_record=False, days_since_contact=0, has_facts=False,
    )


def _profiles(with_past):
    people = {"no organization": PersonEntity(id="0", canonical_name="Pat Zero")}
    for number, org in enumerate(ORGANIZATIONS, start=1):
        people[f"current at {org}"] = PersonEntity(id=f"c{number}", canonical_name=f"Pat Current{number}", company=org)
        past = [{"name": org, "source": "linkedin_profile", "since": "2018", "until": "2021"}] if with_past else []
        people[f"former at {org}"] = PersonEntity(id=f"f{number}", canonical_name=f"Pat Former{number}", company="Elsewhere", past_organizations=past)
    return {label: _descriptive_lines(generate_person_document(p, _summary())) for label, p in people.items()}


def _answers(question, profiles):
    org = next(org for org in ORGANIZATIONS if org.lower() in question.lower())
    return org, {label for label, text in profiles.items() if re.search(rf"\b{re.escape(org)}\b", text)}


def test_every_question_finds_current_and_former_employees_only_through_past_organizations():
    questions = [ln.strip() for ln in QUERIES.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    assert len(questions) >= 6
    with_past, without = _profiles(True), _profiles(False)
    for question in questions:
        org, found = _answers(question, with_past)
        assert found == {f"current at {org}", f"former at {org}"}, question
        assert _answers(question, without)[1] == {f"current at {org}"}, question
