"""
People found by description: the index narrows, the live store has the last word.

A fake index stands in for Chroma and the embedding model. The store, tool and
route are real, against a throwaway database. Every person here is synthetic.
"""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services import person_indexer
from api.services.agent_tools import _lookup_person
from api.services.person_indexer import (
    DESCRIPTION_MIN_SIMILARITY,
    asks_who_fits,
    find_people_by_description,
    search_people_semantic,
)

pytestmark = pytest.mark.unit


class FakeIndex:
    """Answers VectorStore's search with whatever a test indexed."""

    calls: list = []
    hits: list = []
    broken: bool = False

    def __init__(self, collection_name=None):
        if FakeIndex.broken:
            raise ConnectionError("index unavailable")

    def search(self, query, top_k, recency_weight):
        FakeIndex.calls.append({"query": query, "top_k": top_k})
        return list(FakeIndex.hits)


@pytest.fixture
def index(monkeypatch):
    FakeIndex.calls = []
    FakeIndex.hits = []
    FakeIndex.broken = False
    monkeypatch.setattr(person_indexer, "VectorStore", FakeIndex)
    return FakeIndex


@pytest.fixture
def crm(tmp_path, monkeypatch):
    from api.services import entity_resolver, person_entity
    from api.services.entity_resolver import EntityResolver
    from api.services.link_override import LinkOverrideStore
    from api.services.person_entity import PersonEntityStore

    store = PersonEntityStore(str(tmp_path / "crm.db"))
    overrides = LinkOverrideStore(tmp_path / "crm.db")
    monkeypatch.setattr(entity_resolver, "get_link_override_store", lambda: overrides)
    monkeypatch.setattr(person_entity, "_entity_store", store)
    monkeypatch.setattr(person_indexer, "get_person_entity_store", lambda: store)
    monkeypatch.setattr(entity_resolver, "_entity_resolver", EntityResolver(store))
    return store


def add_person(store, name, strength=30.0, hidden=False):
    from api.services.person_entity import PersonEntity

    person = PersonEntity(canonical_name=name, aliases=[])
    person.relationship_strength = strength
    person.hidden = hidden
    return store.add(person)


def hit(person, profile="Works at Lantern Fundraising Partners", similarity=0.5):
    """An index hit whose document has the same layout generate_person_document writes."""
    content = (
        f"# {person.canonical_name}\n"
        "Moderate relationship (strength: 30/100)\n"
        "Category: Work contact\n"
        + (f"{profile}\n" if profile else "")
        + "\n## Communication Channels\n- gmail: 2 interactions in 90 days (dormant)\n"
        "\nLast contact: within the last month"
    )
    return {"file_path": f"person:{person.id}", "content": content, "semantic_score": similarity}


class TestLiveStoreHasTheLastWord:
    def test_a_person_hidden_since_the_last_sync_never_appears(self, crm, index):
        visible = add_person(crm, "Robin Alpha")
        hidden = add_person(crm, "Robin Beta", hidden=True)
        index.hits = [hit(hidden), hit(visible)]

        assert [r["person_id"] for r in search_people_semantic("fundraising")] == [visible.id]

    def test_a_person_merged_since_the_last_sync_never_appears(self, crm, index):
        survivor = add_person(crm, "Robin Alpha")
        merged = add_person(crm, "Robyn Alphabet")
        crm._merged_ids[merged.id] = survivor.id
        crm._bump_get_all_cache_generation()
        index.hits = [hit(merged), hit(survivor)]

        assert [r["person_id"] for r in search_people_semantic("fundraising")] == [survivor.id]

    def test_a_person_deleted_since_the_last_sync_never_appears(self, crm, index):
        visible = add_person(crm, "Robin Alpha")
        index.hits = [{"file_path": "person:gone", "content": "# Gone", "semantic_score": 0.9}, hit(visible)]

        assert [r["person_id"] for r in search_people_semantic("fundraising")] == [visible.id]

    def test_a_description_search_drops_them_too(self, crm, index):
        visible = add_person(crm, "Robin Alpha")
        hidden = add_person(crm, "Robin Beta", hidden=True)
        merged = add_person(crm, "Robin Gamma")
        crm._merged_ids[merged.id] = visible.id
        crm._bump_get_all_cache_generation()
        index.hits = [hit(hidden), hit(merged), hit(visible)]

        assert [m.person.id for m in find_people_by_description("fundraising")] == [visible.id]


class HashingEmbeddings:
    """Same words, same vector, so a query lands on the profiles sharing its words."""

    def _embed(self, text):
        vector = [0.0] * 64
        for word in text.lower().split():
            vector[sum(word.encode()) % 64] += 1.0
        return vector if any(vector) else [1.0] + [0.0] * 63

    def embed_text(self, text):
        return self._embed(text)

    def embed_texts(self, texts):
        return [self._embed(t) for t in texts]


@pytest.fixture
def real_index(monkeypatch):
    """The real VectorStore and index writer over an in-memory Chroma collection."""
    import uuid

    import chromadb

    from api.services.vectorstore import VectorStore

    store = VectorStore.__new__(VectorStore)
    store._collection = chromadb.EphemeralClient().create_collection(
        name=f"people-{uuid.uuid4().hex}", metadata={"hnsw:space": "cosine"}
    )
    store._embedding_service = HashingEmbeddings()
    monkeypatch.setattr(person_indexer, "VectorStore", lambda collection_name=None: store)
    return store


def index_person(person, vector_store, profile):
    from api.services.relationship_summary import RelationshipSummary

    person.company = profile
    summary = RelationshipSummary(
        person_id=person.id, person_name=person.canonical_name, relationship_strength=30.0,
        channels=[], active_channels=[], primary_channel=None, total_interactions_90d=2,
        last_interaction=None, days_since_contact=5,
    )
    assert person_indexer.index_person_to_vectorstore(person, summary, vector_store)


class TestAgainstARealCollection:
    """Entries written by the real indexer and read back through the real search."""

    def test_an_indexed_person_is_found(self, crm, real_index):
        person = add_person(crm, "Robin Alpha")
        index_person(person, real_index, "Fundraising Partners")

        assert [r["person_id"] for r in search_people_semantic("Fundraising Partners")] == [person.id]

    def test_a_description_finds_them_with_the_word_that_matched(self, crm, real_index):
        fits = add_person(crm, "Robin Alpha")
        other = add_person(crm, "Robin Beta")
        index_person(fits, real_index, "Fundraising Partners")
        index_person(other, real_index, "Tidewater Press")

        matches = find_people_by_description("who do I know in fundraising")

        assert matches[0].person.id == fits.id
        assert matches[0].matched_words == ["fundraising"]

    def test_a_person_hidden_after_indexing_is_dropped(self, crm, real_index):
        person = add_person(crm, "Robin Alpha")
        index_person(person, real_index, "Fundraising Partners")
        person.hidden = True
        crm.update(person)

        assert search_people_semantic("Fundraising Partners") == []

    def test_strength_and_category_filter_on_the_live_person(self, crm, real_index):
        person = add_person(crm, "Robin Alpha", strength=30.0)
        person.category = "work"
        crm.update(person)
        index_person(person, real_index, "Fundraising Partners")

        query = "Fundraising Partners"
        assert search_people_semantic(query, min_strength=30.0)
        assert not search_people_semantic(query, min_strength=30.1)
        assert search_people_semantic(query, category="work")
        assert not search_people_semantic(query, category="family")


class TestRanking:
    def test_a_shared_word_outranks_similarity(self, crm, index):
        similar = add_person(crm, "Robin Alpha")
        fits = add_person(crm, "Robin Beta")
        index.hits = [
            hit(similar, "Works at Tidewater Press", DESCRIPTION_MIN_SIMILARITY + 0.2),
            hit(fits, "Works at Lantern Fundraising Partners", 0.3),
        ]

        assert [m.person.id for m in find_people_by_description("who do I know in fundraising")] == [
            fits.id,
            similar.id,
        ]

    def test_more_shared_words_rank_higher(self, crm, index):
        one = add_person(crm, "Robin Alpha")
        two = add_person(crm, "Robin Beta")
        index.hits = [
            hit(one, "Works at Lantern Press", 0.4),
            hit(two, "Works at Lantern Fundraising Partners", 0.3),
        ]

        matches = find_people_by_description("fundraising at lantern")

        assert [m.person.id for m in matches] == [two.id, one.id]

    def test_a_word_only_counts_from_its_start(self, crm, index):
        person = add_person(crm, "Robin Alpha")
        index.hits = [hit(person, "Works at Smart Partners", 0.3)]

        assert find_people_by_description("art") == []

    def test_a_word_form_still_matches(self, crm, index):
        person = add_person(crm, "Robin Alpha")
        index.hits = [hit(person, "Works at Lantern as Fundraiser", 0.3)]

        assert [m.person.id for m in find_people_by_description("fundraising")] == [person.id]


class TestPrecisionFloor:
    @pytest.mark.parametrize(
        ("similarity", "kept"),
        [(DESCRIPTION_MIN_SIMILARITY, True), (DESCRIPTION_MIN_SIMILARITY - 0.01, False)],
    )
    def test_no_shared_word_means_clearing_the_floor(self, crm, index, similarity, kept):
        person = add_person(crm, "Robin Alpha")
        index.hits = [hit(person, "Works at Tidewater Press", similarity)]

        assert bool(find_people_by_description("fundraising")) is kept

    def test_a_profile_with_nothing_descriptive_never_counts(self, crm, index):
        person = add_person(crm, "Fundraising Example")
        index.hits = [hit(person, profile=None, similarity=0.99)]

        assert find_people_by_description("fundraising") == []

    def test_a_name_is_not_descriptive_text(self, crm, index):
        person = add_person(crm, "Fundraising Example")
        index.hits = [hit(person, "Works at Tidewater Press", 0.3)]

        assert find_people_by_description("fundraising") == []

    def test_facts_and_tags_count_as_descriptive(self, crm, index):
        tagged = add_person(crm, "Robin Alpha")
        with_fact = add_person(crm, "Robin Beta")
        index.hits = [
            hit(tagged, "Tags: industry:fundraising", 0.3),
            hit(with_fact, "\n## Known Facts\n- work: raises money for fundraising drives", 0.3),
        ]

        assert {m.person.id for m in find_people_by_description("fundraising")} == {tagged.id, with_fact.id}

    def test_top_k_caps_the_result(self, crm, index):
        people = [add_person(crm, f"Robin {suffix}") for suffix in ("Alpha", "Beta", "Gamma")]
        index.hits = [hit(p) for p in people]

        assert len(find_people_by_description("fundraising", top_k=2)) == 2

    def test_a_question_with_no_content_never_reaches_the_index(self, crm, index):
        assert find_people_by_description("who do I know") == []
        assert index.calls == []


class TestDegradation:
    def test_an_unreachable_index_finds_no_one(self, crm, index):
        index.broken = True

        assert find_people_by_description("fundraising") == []

    def test_a_failure_logs_no_query(self, crm, index, caplog):
        index.broken = True

        find_people_by_description("that secret fundraising lead")

        assert "ConnectionError" in caplog.text
        assert "secret" not in caplog.text


class TestAsksWhoFits:
    @pytest.mark.parametrize("text", [
        "who do I know in fundraising",
        "Who works in venture capital",
        "who is a lawyer",
        "who was that recruiter I talked to",
        "who is in fundraising",
        "who do I know who is a designer",
        "who do I know working in law",
        "who knows about visas",
        "do I know anyone in fundraising",
        "people in fundraising",
        "someone who can help with visas",
        "anyone at Lantern Works",
        "do I know any doctors",
        "which recruiters do I know",
        "that lawyer from the Lantern days",
    ])
    def test_a_search_that_says_it_wants_whoever_fits(self, text):
        assert asks_who_fits(text)

    @pytest.mark.parametrize("text", [
        "Thistlewaite Barrowman",
        "Quillon",
        "Maria de la Cruz",
        "maria de la cruz",
        "Ludwig van Beethoven",
        "Robin Example at Acme",
        "robin example at acme",
        "robin exampel",
        "john smith jr",
        "fundraising",
        "fundraising contacts",
        "doctor who",
        "robin.alpha@example.invalid who",
        "people in robin.alpha@example.invalid",
        "People In Fundraising",
        "Who Works Here",
        "who do I know",
        "",
    ])
    def test_a_name_or_bare_topic_does_not_say_that(self, text):
        assert not asks_who_fits(text)

    @pytest.mark.parametrize("text", [
        "who is Robin Exampel",
        "who is Maria de la Cruz",
        "who is robin exampel",
        "who was Quillon Zzyx",
        "Who is Mary-Anne O'Example?",
        "do I know Maria de la Cruz",
        "do i know robin exampel",
        "tell me who is Robin Exampel",
        "who knows Robin Exampel",
        "which Robin Exampel",
        "that Robin from the Lantern days",
    ])
    def test_the_same_wording_in_front_of_a_name_is_still_naming(self, text):
        assert not asks_who_fits(text)


class TestPersonInfoLookup:
    def test_an_exact_name_goes_first_and_skips_the_index(self, crm, index, monkeypatch):
        person = add_person(crm, "Fundraising Example")
        self._no_profile_stores(monkeypatch)

        out = _lookup_person({"name": "fundraising example"})

        assert person.id in out
        assert index.calls == []

    def test_a_first_name_that_fits_people_skips_the_index(self, crm, index):
        add_person(crm, "Robin Alpha", 60.0)
        add_person(crm, "Robin Beta", 55.0)

        out = _lookup_person({"name": "robin"})

        assert "does not identify one person" in out
        assert index.calls == []

    def test_a_description_reaches_the_index_and_reads_as_suggestions(self, crm, index):
        person = add_person(crm, "Robin Alpha")
        index.hits = [hit(person)]

        out = _lookup_person({"name": "who do I know in fundraising"})

        assert index.calls[0]["query"] == "who do I know in fundraising"
        assert person.id in out
        assert "mentions fundraising" in out
        assert "suggestions to check, not confirmed matches" in out

    def test_a_hidden_person_stays_out_of_the_suggestions(self, crm, index):
        hidden = add_person(crm, "Robin Secret", hidden=True)
        index.hits = [hit(hidden)]

        out = _lookup_person({"name": "who do I know in fundraising"})

        assert "Secret" not in out
        assert "No person found matching" in out

    def test_an_unreachable_index_falls_back_to_the_plain_miss(self, crm, index):
        index.broken = True

        out = _lookup_person({"name": "who do I know in fundraising"})

        assert "No person found matching 'who do I know in fundraising'." in out

    @pytest.mark.parametrize("term", [
        "Thistlewaite Barrowman",
        "thistlewaite barrowman",
        "maria de la cruz",
        "john smith jr",
        "who is Robin Exampel",
        "who is robin exampel",
        "fundraising",
    ])
    def test_a_miss_that_does_not_ask_who_fits_never_reaches_the_index(self, crm, index, term):
        secret = add_person(crm, "Robin Secret")
        index.hits = [hit(secret, "Notes: a private note about their health")]

        out = _lookup_person({"name": term})

        assert "No person found matching" in out
        assert "private note" not in out
        assert index.calls == []

    def test_a_miss_says_how_to_search_by_description(self, crm, index):
        assert "who do I know in" in _lookup_person({"name": "fundraising"})

    def test_a_hidden_full_name_is_a_miss(self, crm, index):
        add_person(crm, "Robin Secret", hidden=True)

        out = _lookup_person({"name": "Robin Secret"})

        assert "No person found matching" in out

    def test_a_first_name_lookup_never_returns_a_profile_on_a_guess(self, crm, index, monkeypatch):
        close = add_person(crm, "Robin Alpha", 90.0)
        add_person(crm, "Robin Beta", 20.0)
        self._no_profile_stores(monkeypatch)

        out = _lookup_person({"name": "Robin"})

        assert "Assuming you mean Robin Alpha" in out
        assert close.id in out
        assert "**Robin Alpha**" not in out

    def _no_profile_stores(self, monkeypatch):
        from api.services import person_facts, relationship_summary

        class NoFacts:
            def get_for_person(self, person_id, include_shared=True):
                return []

        monkeypatch.setattr(person_facts, "get_person_fact_store", lambda: NoFacts())
        monkeypatch.setattr(relationship_summary, "get_relationship_summary", lambda pid: None)


class TestPersonInfoBriefing:
    async def _briefing(self, monkeypatch, name, email=None):
        from api.services import briefings
        from api.services.agent_tools import _briefing_person

        calls = []

        class FakeService:
            async def generate_briefing(self, person_name, email=None, interaction_days=None):
                calls.append(person_name)
                return {"status": "success", "briefing": "## Brief"}

        monkeypatch.setattr(briefings, "get_briefings_service", lambda: FakeService())
        return await _briefing_person({"name": name, "email": email}), calls

    async def test_a_first_name_two_people_share_generates_no_briefing(self, crm, monkeypatch):
        add_person(crm, "Robin Alpha", 60.0)
        add_person(crm, "Robin Beta", 55.0)

        out, calls = await self._briefing(monkeypatch, "Robin")

        assert calls == []
        assert "No briefing generated" in out
        assert "Robin Beta" in out

    async def test_a_likely_first_name_generates_no_briefing(self, crm, monkeypatch):
        add_person(crm, "Robin Alpha", 90.0)
        add_person(crm, "Robin Beta", 20.0)

        out, calls = await self._briefing(monkeypatch, "Robin")

        assert calls == []
        assert "Assuming you mean Robin Alpha" in out

    async def test_a_full_name_is_briefed(self, crm, monkeypatch):
        add_person(crm, "Robin Alpha", 60.0)
        add_person(crm, "Robin Beta", 55.0)

        out, calls = await self._briefing(monkeypatch, "Robin Alpha")

        assert calls == ["Robin Alpha"]
        assert out == "## Brief"

    async def test_an_email_settles_a_first_name(self, crm, monkeypatch):
        add_person(crm, "Robin Alpha", 60.0)
        add_person(crm, "Robin Beta", 55.0)

        _, calls = await self._briefing(monkeypatch, "Robin", email="robin@example.invalid")

        assert calls == ["Robin"]


class TestPeopleSearchRoute:
    @pytest.fixture
    def client(self, crm, monkeypatch):
        from api.routes import people
        from api.services import interaction_store

        class NoInteractions:
            def get_last_interaction_by_source_batch(self, ids):
                return {}

        monkeypatch.setattr(people, "get_person_entity_store", lambda: crm)
        monkeypatch.setattr(interaction_store, "get_interaction_store", lambda: NoInteractions())
        monkeypatch.setattr(people, "get_interaction_store", lambda: NoInteractions())
        app = FastAPI()
        app.include_router(people.router)
        return TestClient(app)

    def test_a_description_returns_people_in_the_usual_shape(self, crm, index, client):
        person = add_person(crm, "Robin Alpha")
        index.hits = [hit(person)]

        body = client.get("/api/people/search", params={"q": "who do I know in fundraising"}).json()

        assert [p["entity_id"] for p in body["people"]] == [person.id]
        assert body["count"] == 1
        assert body["total"] == 1
        assert body["query"] == "who do I know in fundraising"

    def test_a_name_match_goes_first_and_skips_the_index(self, crm, index, client):
        person = add_person(crm, "people in fundraising")
        index.hits = [hit(add_person(crm, "Robin Alpha"))]

        body = client.get("/api/people/search", params={"q": "people in fundraising"}).json()

        assert [p["entity_id"] for p in body["people"]] == [person.id]
        assert index.calls == []

    @pytest.mark.parametrize("q", [
        "robyn alpho",
        "Maria de la Cruz",
        "maria de la cruz",
        "Ludwig van Beethoven",
        "Robin Example at Acme",
        "john smith jr",
        "the fundraising team lead",
        "who is Robin Exampel",
        "who is Maria de la Cruz",
        "who is robin exampel",
        "do I know Maria de la Cruz",
        "fundraising contacts",
    ])
    def test_a_name_that_is_not_found_stays_an_empty_result(self, crm, index, client, q):
        index.hits = [hit(add_person(crm, "Quillon Zzyx"))]

        body = client.get("/api/people/search", params={"q": q}).json()

        assert body["people"] == []
        assert body["count"] == 0
        assert index.calls == []

    def test_a_hidden_person_never_appears(self, crm, index, client):
        index.hits = [hit(add_person(crm, "Robin Secret", hidden=True))]

        body = client.get("/api/people/search", params={"q": "who do I know in fundraising"}).json()

        assert body["people"] == []

    def test_an_unreachable_index_is_an_empty_result(self, crm, index, client):
        index.broken = True

        response = client.get("/api/people/search", params={"q": "who do I know in fundraising"})

        assert response.status_code == 200
        assert response.json()["people"] == []
        assert response.json()["total"] == 0

    def test_results_keep_their_ranking_rather_than_recency(self, crm, index, client):
        first = add_person(crm, "Robin Alpha")
        second = add_person(crm, "Robin Beta")
        crm_conn = crm._get_connection()
        crm_conn.execute(
            "UPDATE person_entities SET last_seen = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), second.id),
        )
        crm_conn.commit()
        crm_conn.close()
        index.hits = [
            hit(first, "Works at Lantern Fundraising Partners", 0.6),
            hit(second, "Works at Lantern Fundraising Partners", 0.5),
        ]

        body = client.get("/api/people/search", params={"q": "who do I know in fundraising"}).json()

        assert [p["entity_id"] for p in body["people"]] == [first.id, second.id]
