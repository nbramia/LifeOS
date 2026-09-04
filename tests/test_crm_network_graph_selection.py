"""
Unit tests for GET /api/crm/network's centered-neighborhood selection logic
(api/routes/crm.py::get_network_graph), using hand-built fixtures and
patched stores rather than a running server or real data (#896 adversarial
review of #870/PR #896).

These call the route handler function directly (bypassing HTTP), following
the pattern established in test_crm_people_list_batching.py for #880.
"""
from unittest.mock import patch

import pytest

from api.services.person_entity import PersonEntity
from api.services.relationship import Relationship
from config.settings import settings

pytestmark = pytest.mark.unit


class _FakeConn:
    """Stands in for a sqlite3.Connection - closeable, otherwise unused."""

    def close(self):
        pass

    def execute(self, *args, **kwargs):
        raise AssertionError("fake connection should not be queried directly")


class _FakePersonStore:
    """Stands in for PersonEntityStore: a fixed people-by-canonical-id map
    plus a legacy-id -> canonical-id merge map, matching the real
    get_canonical_id()/get_by_id()/get_all() contracts the endpoint uses."""

    def __init__(self, people_by_id: dict, merged_ids: dict | None = None):
        self._people_by_id = people_by_id
        self._merged_ids = merged_ids or {}

    def get_canonical_id(self, person_id: str) -> str:
        return self._merged_ids.get(person_id, person_id)

    def get_by_id(self, person_id: str):
        return self._people_by_id.get(self.get_canonical_id(person_id))

    def get_all(self, include_hidden: bool = False, include_merged: bool = False):
        return list(self._people_by_id.values())


class _FakeRelationshipStore:
    """Stands in for RelationshipStore for the centered-selection path:
    a fixed set of the centre's own relationships, plus a per-node map for
    the second-degree expansion's get_top_neighbors() calls."""

    def __init__(self, center_rels=None, neighbor_rels_by_id=None, edges_among=None):
        self._center_rels = center_rels or []
        self._neighbor_rels_by_id = neighbor_rels_by_id or {}
        self._edges_among = edges_among or []

    def open_connection(self):
        return _FakeConn()

    def get_all_for_person(self, person_id, conn=None):
        return list(self._center_rels)

    def get_top_neighbors(self, person_id, limit, conn=None):
        return list(self._neighbor_rels_by_id.get(person_id, []))[:limit]

    def get_edges_among(self, person_ids):
        return list(self._edges_among)

    def get_all_relationships(self, limit=None):
        raise AssertionError(
            "get_all_relationships() must not be called for a centered request"
        )


class _FakeSourceEntityStore:
    """Stands in for SourceEntityStore's batched category lookup."""

    def __init__(self, sources_by_id: dict | None = None):
        self._sources_by_id = sources_by_id or {}

    def get_for_people_batch(self, canonical_person_ids, limit_per_person=50):
        return {cid: self._sources_by_id.get(cid, []) for cid in canonical_person_ids}


def _call_network_graph(person_store, rel_store, source_store=None, **overrides):
    """Call get_network_graph() directly with patched stores and sensible
    defaults for the query params it would otherwise get from FastAPI's
    Query(...) declarations."""
    from api.routes.crm import get_network_graph

    kwargs = dict(
        center_on="center", depth=2, min_strength=0.0, category=None,
        max_nodes=150, max_second_degree_per_node=10, max_edges=2000,
        allow_full_graph=False,
    )
    kwargs.update(overrides)

    with patch("api.routes.crm.get_person_entity_store", return_value=person_store), \
            patch("api.routes.crm.get_relationship_store", return_value=rel_store), \
            patch("api.routes.crm.get_source_entity_store", return_value=source_store or _FakeSourceEntityStore()):
        return get_network_graph(**kwargs)


class TestLegacyIdNeighbor:
    """#896 review finding 1: a relationship row whose neighbor endpoint is
    a merged-away (legacy) id must not produce a duplicate node id, must
    still connect to the centre, and must compute the same edge weight as
    the pre-#870 formula would for the same real-world pair."""

    def test_unique_node_and_centre_edge_for_non_owner_pair(self, monkeypatch):
        monkeypatch.setattr(settings, "my_person_id", "owner-not-in-graph")

        center = PersonEntity(id="center", canonical_name="Center")
        neighbor = PersonEntity(id="canonical-neighbor", canonical_name="Neighbor")

        # The stored relationship row references the LEGACY id, not the
        # canonical one - simulating an incompletely-migrated old merge.
        legacy_rel = Relationship(
            person_a_id="center", person_b_id="legacy-neighbor-id",
            shared_events_count=3,
        )

        person_store = _FakePersonStore(
            people_by_id={"center": center, "canonical-neighbor": neighbor},
            merged_ids={"legacy-neighbor-id": "canonical-neighbor"},
        )
        rel_store = _FakeRelationshipStore(center_rels=[legacy_rel])

        result = _call_network_graph(person_store, rel_store, depth=1)

        node_ids = [n.id for n in result.nodes]
        assert len(node_ids) == len(set(node_ids)), "duplicate node id"
        assert "canonical-neighbor" in node_ids
        assert "legacy-neighbor-id" not in node_ids

        edge_pairs = {frozenset((e.source, e.target)) for e in result.edges}
        assert frozenset(("center", "canonical-neighbor")) in edge_pairs

        # Neither side is the owner, so the OLD formula (and the fixed one)
        # both use pair_strength here - this pins that the edge is present
        # and correctly weighted, not silently dropped.
        matching = [e for e in result.edges
                    if frozenset((e.source, e.target)) == frozenset(("center", "canonical-neighbor"))]
        assert len(matching) == 1
        assert matching[0].weight == legacy_rel.pair_strength

    def test_owner_edge_weight_uses_canonical_relationship_strength(self, monkeypatch):
        """The specific numeric bug from the review: looking up the OTHER
        person by their legacy id in a canonical-keyed people dict misses
        and silently falls back to pair_strength instead of their real
        relationship_strength."""
        monkeypatch.setattr(settings, "my_person_id", "owner-id")

        owner = PersonEntity(id="owner-id", canonical_name="Owner")
        neighbor = PersonEntity(id="canonical-neighbor", canonical_name="Neighbor")
        neighbor.relationship_strength = 77.0

        legacy_rel = Relationship(
            person_a_id="owner-id", person_b_id="legacy-neighbor-id",
            shared_events_count=1,
        )
        # Sanity: the fixture must actually distinguish the two formulas.
        assert int(neighbor.relationship_strength) != legacy_rel.pair_strength

        person_store = _FakePersonStore(
            people_by_id={"owner-id": owner, "canonical-neighbor": neighbor},
            merged_ids={"legacy-neighbor-id": "canonical-neighbor"},
        )
        rel_store = _FakeRelationshipStore(center_rels=[legacy_rel])

        result = _call_network_graph(person_store, rel_store, center_on="owner-id", depth=1)

        edge = next(
            e for e in result.edges
            if frozenset((e.source, e.target)) == frozenset(("owner-id", "canonical-neighbor"))
        )
        assert edge.weight == int(neighbor.relationship_strength)


class TestSecondDegreeTier:
    """#896 review finding 3: depth>=2 must still surface second-degree
    nodes for a well-connected centre, instead of first-degree selection
    silently consuming the whole max_nodes budget."""

    def test_second_degree_nodes_appear_for_well_connected_centre(self, monkeypatch):
        monkeypatch.setattr(settings, "my_person_id", "nobody")

        people = {"center": PersonEntity(id="center", canonical_name="Center")}
        center_rels = []
        neighbor_rels_by_id = {}
        for i in range(7):
            first_id = f"first{i}"
            second_id = f"second{i}"
            people[first_id] = PersonEntity(id=first_id, canonical_name=first_id)
            people[second_id] = PersonEntity(id=second_id, canonical_name=second_id)
            center_rels.append(Relationship(
                person_a_id="center", person_b_id=first_id, shared_events_count=10 - i,
            ))
            neighbor_rels_by_id[first_id] = [Relationship(
                person_a_id=first_id, person_b_id=second_id, shared_events_count=5,
            )]

        person_store = _FakePersonStore(people_by_id=people)
        rel_store = _FakeRelationshipStore(center_rels=center_rels, neighbor_rels_by_id=neighbor_rels_by_id)

        result = _call_network_graph(
            person_store, rel_store, depth=2, max_nodes=10, max_second_degree_per_node=5,
        )

        degrees_present = {n.degree for n in result.nodes}
        assert 2 in degrees_present, "expected at least one second-degree node"
        assert len(result.nodes) <= 10

        first_degree_count = sum(1 for n in result.nodes if n.degree == 1)
        # 75% of max_nodes=10 -> 7, so first-degree must not consume all 9
        # non-centre slots the old code would have given it.
        assert first_degree_count <= 7

    def test_depth_one_uses_full_budget_for_first_degree(self, monkeypatch):
        """depth=1 has no deeper tier, so it should NOT apply the 75% split
        -- first-degree gets the full remaining budget, matching pre-#896
        behavior for a one-hop request."""
        monkeypatch.setattr(settings, "my_person_id", "nobody")

        people = {"center": PersonEntity(id="center", canonical_name="Center")}
        center_rels = []
        for i in range(9):
            first_id = f"first{i}"
            people[first_id] = PersonEntity(id=first_id, canonical_name=first_id)
            center_rels.append(Relationship(
                person_a_id="center", person_b_id=first_id, shared_events_count=10 - i,
            ))

        person_store = _FakePersonStore(people_by_id=people)
        rel_store = _FakeRelationshipStore(center_rels=center_rels)

        result = _call_network_graph(person_store, rel_store, depth=1, max_nodes=10)

        first_degree_count = sum(1 for n in result.nodes if n.degree == 1)
        assert first_degree_count == 9  # all 9 candidates fit max_nodes-1

    def test_dense_centre_still_returns_genuine_second_degree_nodes(self, monkeypatch):
        """#896 review round 4, MAJOR finding: the reserved second-degree
        budget was being reclaimed by direct connections that missed the
        first-degree cut (re-discovered through a friend, then promoted
        back to degree 1 by the finding-4 relabelling), leaving nothing
        for genuine friends-of-friends on a dense centre. Fixed by skipping
        ids already in all_direct_candidates during deeper-hop expansion."""
        monkeypatch.setattr(settings, "my_person_id", "nobody")

        people = {"center": PersonEntity(id="center", canonical_name="Center")}
        center_rels = []
        # 12 genuine direct candidates - max_nodes=10 with depth>=2 caps
        # first-degree at 7 (75%), so 5 of them ("missed0".."missed4")
        # are real direct connections that just didn't rank high enough.
        for i in range(12):
            pid = f"first{i}" if i < 7 else f"missed{i - 7}"
            people[pid] = PersonEntity(id=pid, canonical_name=pid)
            center_rels.append(Relationship(
                person_a_id="center", person_b_id=pid, shared_events_count=30 - i,
            ))

        # Each selected first-degree node also knows one of the "missed"
        # direct candidates (the old bug's re-entry path) AND one genuine
        # friend-of-friend who has no relationship with the centre at all.
        neighbor_rels_by_id = {}
        for i in range(7):
            first_id = f"first{i}"
            missed_id = f"missed{i % 5}"
            friend_id = f"genuine_friend{i}"
            people[friend_id] = PersonEntity(id=friend_id, canonical_name=friend_id)
            neighbor_rels_by_id[first_id] = [
                Relationship(person_a_id=first_id, person_b_id=missed_id, shared_events_count=10),
                Relationship(person_a_id=first_id, person_b_id=friend_id, shared_events_count=9),
            ]

        person_store = _FakePersonStore(people_by_id=people)
        rel_store = _FakeRelationshipStore(center_rels=center_rels, neighbor_rels_by_id=neighbor_rels_by_id)

        result = _call_network_graph(
            person_store, rel_store, depth=2, max_nodes=10, max_second_degree_per_node=5,
        )

        degree2_ids = {n.id for n in result.nodes if n.degree == 2}
        assert degree2_ids, "expected at least one genuine second-degree node"
        assert not any(nid.startswith("missed") for nid in degree2_ids), (
            "a direct connection that missed the first-degree cut must never "
            "be labeled degree 2"
        )
        assert any(nid.startswith("genuine_friend") for nid in degree2_ids)


class TestFirstDegreeRankedByRenderedWeight:
    """#896 review finding 5: replaces the old circular test (which compared
    the endpoint's output against get_top_neighbors, the very function it
    used to rank candidates, so it could not fail on a wrong ranking).
    Uses a hand-built fixture with a KNOWN correct order by the real
    rendered edge weight, deliberately opposite the shared-count-sum proxy's
    order, so a proxy-based implementation would fail this test."""

    def test_ranks_by_pair_strength_not_shared_count_sum(self, monkeypatch):
        from datetime import datetime, timedelta, timezone

        monkeypatch.setattr(settings, "my_person_id", "nobody")  # pair_strength branch

        now = datetime.now(timezone.utc)
        # "bulk": huge raw shared-count sum (wins under the old proxy) but
        # old and single-source -> LOW pair_strength.
        bulk = Relationship(
            person_a_id="center", person_b_id="bulk",
            shared_slack_count=50, last_seen_together=now - timedelta(days=250),
        )
        # "recent": small raw count sum (loses under the old proxy) but
        # very recent and multi-source -> HIGH pair_strength.
        recent = Relationship(
            person_a_id="center", person_b_id="recent",
            shared_events_count=3, shared_phone_calls_count=3, last_seen_together=now,
        )
        # Sanity: the fixture actually inverts the two rankings, or this
        # test would pass regardless of which one the code uses.
        assert bulk.total_shared_interactions > recent.total_shared_interactions
        assert recent.pair_strength > bulk.pair_strength

        person_store = _FakePersonStore(people_by_id={
            "center": PersonEntity(id="center", canonical_name="Center"),
            "bulk": PersonEntity(id="bulk", canonical_name="Bulk"),
            "recent": PersonEntity(id="recent", canonical_name="Recent"),
        })
        rel_store = _FakeRelationshipStore(center_rels=[bulk, recent])

        # max_nodes=2 -> first_degree_limit = max_nodes-1 = 1 (depth=1), so
        # only the true strongest by rendered weight survives.
        result = _call_network_graph(
            person_store, rel_store, depth=1, max_nodes=2, max_second_degree_per_node=0,
        )

        first_degree_ids = [n.id for n in result.nodes if n.degree == 1]
        assert first_degree_ids == ["recent"]


class TestMaxEdgesAlwaysIncludesCentreEdges:
    """#896 review findings 2 and 4: max_edges bounds the TOTAL edges
    returned, but every edge touching the centre is unconditionally
    included even if that alone exceeds max_edges."""

    def test_centre_edges_survive_a_tiny_max_edges(self, monkeypatch):
        monkeypatch.setattr(settings, "my_person_id", "nobody")

        people = {"center": PersonEntity(id="center", canonical_name="Center")}
        center_rels = []
        for i in range(5):
            first_id = f"first{i}"
            people[first_id] = PersonEntity(id=first_id, canonical_name=first_id)
            center_rels.append(Relationship(person_a_id="center", person_b_id=first_id, shared_events_count=1))

        person_store = _FakePersonStore(people_by_id=people)
        rel_store = _FakeRelationshipStore(center_rels=center_rels)

        result = _call_network_graph(
            person_store, rel_store, depth=1, max_nodes=150, max_edges=2,
        )

        # All 5 centre edges present despite max_edges=2.
        assert len(result.edges) == 5
        edge_pairs = {frozenset((e.source, e.target)) for e in result.edges}
        for i in range(5):
            assert frozenset(("center", f"first{i}")) in edge_pairs

    def test_remaining_budget_fills_with_strongest_non_centre_edges(self, monkeypatch):
        """Once every centre edge is included, the leftover max_edges budget
        goes to the strongest remaining edges among the selected nodes, not
        an arbitrary subset."""
        monkeypatch.setattr(settings, "my_person_id", "nobody")

        people = {
            "center": PersonEntity(id="center", canonical_name="Center"),
            "a": PersonEntity(id="a", canonical_name="A"),
            "b": PersonEntity(id="b", canonical_name="B"),
            "c": PersonEntity(id="c", canonical_name="C"),
        }
        center_rels = [
            Relationship(person_a_id="center", person_b_id="a", shared_events_count=1),
            Relationship(person_a_id="center", person_b_id="b", shared_events_count=1),
            Relationship(person_a_id="center", person_b_id="c", shared_events_count=1),
        ]
        # Two non-centre edges among a/b/c, different strengths.
        weak = Relationship(person_a_id="a", person_b_id="b", shared_events_count=1)
        strong = Relationship(person_a_id="a", person_b_id="c", shared_events_count=50)
        assert strong.pair_strength > weak.pair_strength

        person_store = _FakePersonStore(people_by_id=people)
        rel_store = _FakeRelationshipStore(center_rels=center_rels, edges_among=[weak, strong])

        # 3 centre edges + budget for exactly 1 more non-centre edge.
        result = _call_network_graph(
            person_store, rel_store, depth=1, max_nodes=150, max_edges=4,
        )

        assert len(result.edges) == 4
        edge_pairs = {frozenset((e.source, e.target)) for e in result.edges}
        assert frozenset(("a", "c")) in edge_pairs  # the stronger one
        assert frozenset(("a", "b")) not in edge_pairs  # the weaker one, dropped


class TestCentreEdgeSkippedWhenCentreFiltered:
    """#896 review round 3, MAJOR finding 3: emitting centre edges
    unconditionally after only checking the OTHER endpoint broke edge
    closure whenever the centre itself didn't survive the category/
    min_strength filter."""

    def test_no_edge_names_the_filtered_out_centre(self, monkeypatch):
        monkeypatch.setattr(settings, "my_person_id", "nobody")

        # Centre has no work signal at all -> categorised "personal" and
        # will be filtered out by category="work" below. The first-degree
        # candidate has a "work" signal (slack) so it survives selection.
        center = PersonEntity(id="center", canonical_name="Center")
        first = PersonEntity(id="first", canonical_name="First", sources=["slack"])

        center_rels = [Relationship(person_a_id="center", person_b_id="first", shared_events_count=5)]

        person_store = _FakePersonStore(people_by_id={"center": center, "first": first})
        rel_store = _FakeRelationshipStore(center_rels=center_rels)

        result = _call_network_graph(
            person_store, rel_store, depth=1, max_nodes=150, category="work",
        )

        node_ids = {n.id for n in result.nodes}
        assert "center" not in node_ids  # filtered out, as intended
        assert "first" in node_ids

        for edge in result.edges:
            assert edge.source in node_ids
            assert edge.target in node_ids
        assert not any("center" in (e.source, e.target) for e in result.edges)


class TestDegreeRelabeling:
    """#896 review round 3, MINOR finding 4, as refined by round 4: `degree`
    must mean "has a direct edge to the centre." Round 3 achieved this by
    relabelling a direct connection that missed the first-degree cut back
    to degree 1 if a friend happened to re-introduce it as "second-degree" -
    but round 4 found that re-entry path was reclaiming the whole budget
    reserved for genuine friends-of-friends on a dense centre. The deeper-hop
    expansion now skips any id that's already a known direct connection of
    the centre (see TestSecondDegreeTier's dense-centre test), so a missed
    direct connection is no longer re-discoverable at all - it's simply
    absent from a budget-constrained response, exactly like any other
    candidate that didn't make the cut. `all_direct_candidates` is kept in
    `api/routes/crm.py` as a relabelling backstop that should no longer be
    reachable in practice; this test locks down the now-correct behavior
    (absence, not relabelling) for the exact scenario round 3's test used."""

    def test_direct_neighbor_that_missed_the_cut_is_simply_absent(self, monkeypatch):
        monkeypatch.setattr(settings, "my_person_id", "nobody")

        people = {"center": PersonEntity(id="center", canonical_name="Center")}
        center_rels = []
        # 8 genuine direct candidates; max_nodes=10 with depth>=2 caps
        # first-degree at 75% = 7, so the weakest ("missed") gets excluded
        # from the first pass despite having a real edge to the centre.
        for i in range(8):
            pid = f"first{i}" if i < 7 else "missed"
            people[pid] = PersonEntity(id=pid, canonical_name=pid)
            # Descending strength: first0 strongest, "missed" weakest.
            center_rels.append(Relationship(
                person_a_id="center", person_b_id=pid, shared_events_count=20 - i,
            ))

        # first0 (the strongest, definitely selected) is also directly
        # connected to "missed" - this is the round-3 re-entry path, which
        # must now be skipped rather than followed.
        friend_rel = Relationship(person_a_id="first0", person_b_id="missed", shared_events_count=3)

        person_store = _FakePersonStore(people_by_id=people)
        rel_store = _FakeRelationshipStore(
            center_rels=center_rels,
            neighbor_rels_by_id={"first0": [friend_rel]},
        )

        result = _call_network_graph(
            person_store, rel_store, depth=2, max_nodes=10, max_second_degree_per_node=5,
        )

        node_ids = {n.id for n in result.nodes}
        assert "missed" not in node_ids

        edge_pairs = {frozenset((e.source, e.target)) for e in result.edges}
        assert frozenset(("center", "missed")) not in edge_pairs


class TestCategoryPredicateConsistency:
    """#896 review round 3, MINOR finding 5: selection used
    compute_person_category() with batched source entities, while the
    node-building filter re-checked with a different (raw attribute)
    predicate -- a candidate could be selected under one decision and then
    dropped under another. Fixed by having the node-building filter trust
    an id already confirmed during selection instead of re-deriving a
    possibly different answer."""

    def test_candidate_confirmed_via_batched_sources_is_not_dropped_by_the_cheap_check(self, monkeypatch):
        from api.services.source_entity import SourceEntity

        monkeypatch.setattr(settings, "my_person_id", "nobody")

        # No email/sources signal of its own -- compute_person_category(p, [])
        # (the cheap check) would call this "personal." Only the batched
        # source-entity fetch (a Slack source entity) reveals "work."
        candidate = PersonEntity(id="candidate", canonical_name="Candidate")
        center = PersonEntity(id="center", canonical_name="Center")

        center_rels = [Relationship(person_a_id="center", person_b_id="candidate", shared_events_count=5)]

        person_store = _FakePersonStore(people_by_id={"center": center, "candidate": candidate})
        rel_store = _FakeRelationshipStore(center_rels=center_rels)
        source_store = _FakeSourceEntityStore(
            sources_by_id={"candidate": [SourceEntity(source_type="slack")]}
        )

        # Sanity: the cheap and batched computations really do disagree for
        # this fixture, or the test proves nothing.
        from api.services.person_entity import compute_person_category
        assert compute_person_category(candidate, []) != "work"
        assert compute_person_category(candidate, [SourceEntity(source_type="slack")]) == "work"

        result = _call_network_graph(
            person_store, rel_store, source_store=source_store,
            depth=1, max_nodes=150, category="work",
        )

        node_ids = {n.id for n in result.nodes}
        assert "candidate" in node_ids
