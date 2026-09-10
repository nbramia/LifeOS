"""Deterministic shard partitioning for the candidate verification gate.

The CI matrix runs several concurrent shards of one lane instead of one job
running the whole lane serially. Correctness rests entirely on the
partition: every shard's node IDs must come from one deterministic sorted
order, the shards together must equal the lane's full collected set with no
overlap, and a shard that would select nothing must fail loudly.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.verify_candidate import (
    CandidateVerificationError, _expected, _parse_shard, _main,
    collect_lane_inventory, make_hermetic_environment, shard_nodeids,
)


REPO = Path(__file__).resolve().parent.parent
SYNTHETIC_UNIT_TEST_COUNT = 23  # deliberately not a multiple of 2, 3, or 4


def _synthetic_snapshot(tmp_path: Path, count: int = SYNTHETIC_UNIT_TEST_COUNT) -> Path:
    """A real snapshot ``collect_lane_inventory`` can collect against: just
    enough of the production plugin/registry it imports, plus ``count``
    synthetic ``unit``-marked tests -- real collection through the shipped
    plugin, without paying for the whole repository's ~7,900-test lane."""
    snapshot = tmp_path / "snapshot"
    (snapshot / "scripts").mkdir(parents=True)
    (snapshot / "tests").mkdir()
    for name in ("test_lane_plugin.py", "test_lane_registry.py", "verification_evidence.py"):
        shutil.copy2(REPO / "scripts" / name, snapshot / "scripts" / name)
    (snapshot / "pyproject.toml").write_text("[tool.pytest.ini_options]\nmarkers = ['unit']\n")
    body = "import pytest\n\n" + "\n".join(
        f"@pytest.mark.unit\ndef test_case_{i}(): assert True\n\n" for i in range(count)
    )
    (snapshot / "tests" / "test_synthetic_cases.py").write_text(body)
    return snapshot


def _collect(tmp_path: Path, *, count: int = SYNTHETIC_UNIT_TEST_COUNT, label: str = "collect") -> dict:
    snapshot = _synthetic_snapshot(tmp_path / label, count)
    environment = make_hermetic_environment(tmp_path / f"{label}-runtime", workers=1)
    return collect_lane_inventory(snapshot, tmp_path / f"{label}-receipts", environment)


@pytest.fixture(scope="module")
def real_inventory(tmp_path_factory) -> dict:
    """One real pytest collection through the production lane plugin, reused
    across every partition-shape assertion below so exercising the whole
    shard space stays fast rather than recollecting per test."""
    tmp_path = tmp_path_factory.mktemp("shard-inventory")
    return _collect(tmp_path)


def _lane_nodeids(inventory: dict) -> tuple[str, ...]:
    return tuple(inventory["lanes"]["fast-unit"]["nodeids"])


@pytest.mark.unit
def test_real_synthetic_collection_produced_every_case(real_inventory):
    """Sanity check on the fixture itself before trusting partitions of it."""
    assert real_inventory["status"] == "ok"
    assert len(_lane_nodeids(real_inventory)) == SYNTHETIC_UNIT_TEST_COUNT


@pytest.mark.unit
@pytest.mark.parametrize("shard_count", [1, 2, 3, 4, 5, 7, SYNTHETIC_UNIT_TEST_COUNT])
def test_shards_are_total_and_disjoint_over_a_real_collected_inventory(real_inventory, shard_count):
    """The union of every shard equals the lane's full collected set, and no
    two shards share a node ID -- asserted over a real collected inventory,
    not by inspection of the partition function alone."""
    whole = set(_lane_nodeids(real_inventory))
    slices = [
        _expected(real_inventory, required_lanes=["fast-unit"], shard=(i, shard_count))["fast-unit"]
        for i in range(shard_count)
    ]
    union: set[str] = set()
    for selected in slices:
        assert selected, "a shard within range must never select nothing"
        assert not (union & set(selected)), "shards must not overlap"
        union |= set(selected)
    assert union == whole


@pytest.mark.unit
def test_shard_count_of_one_reproduces_the_unsharded_selection_exactly(real_inventory):
    """The unsharded path stays exercised: shard 0 of 1 is bit-identical to
    an ordinary (unsharded) selection, not merely an equivalent set."""
    unsharded = _expected(real_inventory, required_lanes=["fast-unit"])["fast-unit"]
    single_shard = _expected(real_inventory, required_lanes=["fast-unit"], shard=(0, 1))["fast-unit"]
    assert single_shard == unsharded


@pytest.mark.unit
def test_sharding_never_changes_lane_membership(real_inventory):
    """Sharding redistributes an existing lane's tests; it must never add,
    drop, or otherwise change which tests belong to the lane. The union of
    a 4-way shard must equal today's unsharded selection of the same lane
    exactly, node ID for node ID."""
    unsharded = set(_expected(real_inventory, required_lanes=["fast-unit"])["fast-unit"])
    sharded_union: set[str] = set()
    for i in range(4):
        sharded_union |= set(_expected(real_inventory, required_lanes=["fast-unit"], shard=(i, 4))["fast-unit"])
    assert sharded_union == unsharded


@pytest.mark.unit
def test_shard_selection_is_deterministic_across_independent_collections(tmp_path):
    """Test collection is not guaranteed byte-for-byte reproducible between
    runs in general; this proves that two independent real collections of
    the identical source still produce the identical shard 2-of-4 selection,
    so a coverage gap from divergent collection would be visible rather than
    silently assumed away."""
    first = _collect(tmp_path, label="first")
    second = _collect(tmp_path, label="second")
    assert _lane_nodeids(first) == _lane_nodeids(second)
    left = _expected(first, required_lanes=["fast-unit"], shard=(2, 4))["fast-unit"]
    right = _expected(second, required_lanes=["fast-unit"], shard=(2, 4))["fast-unit"]
    assert left == right


@pytest.mark.unit
def test_empty_shard_slice_fails_loudly_instead_of_a_vacuous_pass(real_inventory):
    """Requesting more shards than there are tests must fail closed for the
    shards that land past the end, never silently report nothing to run as
    a pass."""
    shard_count = SYNTHETIC_UNIT_TEST_COUNT * 2
    with pytest.raises(CandidateVerificationError, match="selected zero"):
        _expected(real_inventory, required_lanes=["fast-unit"], shard=(SYNTHETIC_UNIT_TEST_COUNT, shard_count))


@pytest.mark.unit
@pytest.mark.parametrize(
    "shard_index, shard_count",
    [(-1, 4), (4, 4), (5, 4), (0, 0), (0, -1), (2, 1)],
)
def test_out_of_range_shard_arguments_are_rejected_before_any_collection(shard_index, shard_count):
    """Index/count validity depends only on the two integers, not on any
    collected data -- so it rejects before a snapshot, subprocess, or
    collection ever starts."""
    with pytest.raises(CandidateVerificationError):
        shard_nodeids(("a", "b", "c"), shard_index, shard_count)
    with pytest.raises(CandidateVerificationError):
        _parse_shard(shard_index, shard_count)


@pytest.mark.unit
def test_parse_shard_requires_both_index_and_count_together():
    assert _parse_shard(None, None) is None
    assert _parse_shard(1, 4) == (1, 4)
    with pytest.raises(CandidateVerificationError):
        _parse_shard(1, None)
    with pytest.raises(CandidateVerificationError):
        _parse_shard(None, 4)


@pytest.mark.unit
def test_cli_rejects_an_out_of_range_shard_before_touching_the_repository(tmp_path, capsys):
    """``tmp_path`` is not a git repository at all -- if shard validation ran
    after repository access, this would fail with a git error instead."""
    rc = _main([
        "pushed-ref", "--repository", str(tmp_path), "--sha", "deadbeef",
        "--shard-index", "9", "--shard-count", "4",
    ])
    assert rc == 1
    assert "shard" in capsys.readouterr().err.lower()


@pytest.mark.unit
def test_cli_local_rejects_a_zero_shard_count_before_starting_a_candidate_owner(tmp_path, capsys):
    rc = _main(["local", "--source", str(tmp_path), "--shard-index", "0", "--shard-count", "0"])
    assert rc == 1
    assert "shard" in capsys.readouterr().err.lower()
