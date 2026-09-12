"""Deterministic partitioning for the candidate verification gate.

The CI matrix runs several concurrent parts of one lane instead of one job
running the whole lane serially. Correctness rests entirely on the
partition, in two alternative units.

A node-ID shard is a contiguous slice of one sorted order: every shard's
node IDs come from that order, the shards together equal the lane's full
collected set with no overlap, and a shard that would select nothing fails
loudly.

A module-scope part is a set of whole modules, never a fraction of one. That
is the unit a partitioned gate run uses, because a test separated from state
its own module established takes a conditional skip it would not take
unpartitioned, and the verifier requires every requested node ID to report
`passed`. So a part must satisfy the shard properties *and* keep every
module whole, produce the same total skip count as an unpartitioned run of
the same tests, and balance by recorded duration rather than test count.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from scripts.test_lane_registry import BY_NAME, marker, scope_of
from scripts.verify_candidate import (
    SCOPE_DURATION_RECORD, CandidateVerificationError, _expected, _parse_part,
    _parse_shard, _main, collect_lane_inventory, load_scope_durations,
    make_hermetic_environment, merge_scope_durations, partition_lane_scopes,
    shard_nodeids,
)


REPO = Path(__file__).resolve().parent.parent
SYNTHETIC_UNIT_TEST_COUNT = 23  # deliberately not a multiple of 2, 3, or 4
# Module name suffix -> test count. Seven modules of coprime sizes, so a
# count-balanced and a duration-balanced assignment are visibly different
# partitions of the same inventory rather than coincidentally equal.
SCOPED_MODULE_SIZES = {"aaa": 1, "bbb": 2, "ccc": 3, "ddd": 5, "eee": 7, "fff": 11, "ggg": 13}


def _synthetic_snapshot(tmp_path: Path, count: int = SYNTHETIC_UNIT_TEST_COUNT) -> Path:
    """A real snapshot ``collect_lane_inventory`` can collect against: just
    enough of the production plugin/registry it imports, plus ``count``
    synthetic ``unit``-marked tests -- real collection through the shipped
    plugin, without paying for the whole repository's ~7,900-test lane."""
    snapshot = tmp_path / "snapshot"
    _write_snapshot_shell(snapshot)
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


def _write_snapshot_shell(snapshot: Path, markers: Sequence[str] = ("unit",)) -> None:
    """The minimum real snapshot the shipped lane plugin can be collected in."""
    (snapshot / "scripts").mkdir(parents=True)
    (snapshot / "tests").mkdir()
    for name in ("test_lane_plugin.py", "test_lane_registry.py", "verification_evidence.py"):
        shutil.copy2(REPO / "scripts" / name, snapshot / "scripts" / name)
    (snapshot / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\nmarkers = [" + ", ".join(repr(name) for name in markers) + "]\n"
    )


def _scoped_snapshot(tmp_path: Path) -> Path:
    """Several ``unit``-marked modules of deliberately unequal size, so a
    partition of whole modules has real scopes to distribute."""
    snapshot = tmp_path / "snapshot"
    _write_snapshot_shell(snapshot)
    for suffix, count in SCOPED_MODULE_SIZES.items():
        body = "import pytest\n\n" + "\n".join(
            f"@pytest.mark.unit\ndef test_case_{i}(): assert True\n\n" for i in range(count)
        )
        (snapshot / "tests" / f"test_scope_{suffix}.py").write_text(body)
    return snapshot


def _collect_snapshot(snapshot: Path, tmp_path: Path, label: str = "collect") -> dict:
    environment = make_hermetic_environment(tmp_path / f"{label}-runtime", workers=1)
    return collect_lane_inventory(snapshot, tmp_path / f"{label}-receipts", environment)


@pytest.fixture(scope="module")
def scoped_inventory(tmp_path_factory) -> dict:
    """One real collection of the multi-module snapshot, reused across every
    part-shape assertion below."""
    tmp_path = tmp_path_factory.mktemp("part-inventory")
    return _collect_snapshot(_scoped_snapshot(tmp_path), tmp_path)


def _scoped_module(suffix: str) -> str:
    return f"tests/test_scope_{suffix}.py"


def _part(inventory: dict, index: int, count: int, durations: Mapping[str, float] | None = None) -> tuple[str, ...]:
    selected = _expected(
        inventory, required_lanes=["fast-unit"], part=(index, count),
        scope_durations={} if durations is None else durations,
    )
    return selected["fast-unit"]


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


@pytest.mark.unit
def test_real_multi_module_collection_produced_every_module(scoped_inventory):
    """Sanity check on the multi-module fixture before trusting parts of it."""
    assert scoped_inventory["status"] == "ok"
    nodeids = _lane_nodeids(scoped_inventory)
    assert len(nodeids) == sum(SCOPED_MODULE_SIZES.values())
    assert {scope_of(nodeid) for nodeid in nodeids} == {
        _scoped_module(suffix) for suffix in SCOPED_MODULE_SIZES
    }


@pytest.mark.unit
@pytest.mark.parametrize("part_count", [1, 2, 3, 4, 5, 7])
def test_parts_are_total_and_disjoint_over_a_real_collected_inventory(scoped_inventory, part_count):
    """The union of every part equals the lane's full collected set, and no
    two parts share a node ID -- asserted over a real collected inventory,
    not by inspection of the assignment function alone."""
    whole = set(_lane_nodeids(scoped_inventory))
    union: set[str] = set()
    for index in range(part_count):
        selected = _part(scoped_inventory, index, part_count)
        assert selected, "a part within range must never select nothing"
        assert not (union & set(selected)), "parts must not overlap"
        union |= set(selected)
    assert union == whole


@pytest.mark.unit
@pytest.mark.parametrize("part_count", [2, 3, 4, 5, 7])
def test_every_module_lands_in_exactly_one_part(scoped_inventory, part_count):
    """The property the whole partition unit exists for: a module's tests
    travel together, so no test is separated from state its own module
    established elsewhere."""
    owners: dict[str, set[int]] = {}
    for index in range(part_count):
        for nodeid in _part(scoped_inventory, index, part_count):
            owners.setdefault(scope_of(nodeid), set()).add(index)
    assert owners, "the partition must own every collected module"
    split = {module: sorted(parts) for module, parts in owners.items() if len(parts) != 1}
    assert not split, f"these modules were split across parts: {split}"


@pytest.mark.unit
def test_part_count_of_one_reproduces_the_unpartitioned_selection_exactly(scoped_inventory):
    """The unpartitioned path stays exercised: part 0 of 1 is bit-identical
    to an ordinary selection, not merely an equivalent set."""
    unpartitioned = _expected(scoped_inventory, required_lanes=["fast-unit"])["fast-unit"]
    assert _part(scoped_inventory, 0, 1) == unpartitioned


@pytest.mark.unit
def test_part_selection_is_deterministic_across_independent_collections(tmp_path):
    """Two independent real collections of identical source must produce the
    identical part 2-of-4 selection, so a coverage gap from divergent
    collection or an order-dependent assignment would be visible rather than
    silently assumed away."""
    first = _collect_snapshot(_scoped_snapshot(tmp_path / "first"), tmp_path, label="first")
    second = _collect_snapshot(_scoped_snapshot(tmp_path / "second"), tmp_path, label="second")
    assert _lane_nodeids(first) == _lane_nodeids(second)
    assert _part(first, 2, 4) == _part(second, 2, 4)


@pytest.mark.unit
def test_part_assignment_balances_recorded_duration_rather_than_test_count(scoped_inventory):
    """With a duration recorded for every module, the parts must come out
    even in *seconds*. The record deliberately makes the one-test module the
    most expensive, so a count-balanced assignment cannot also be
    duration-balanced -- the parts' test counts must therefore differ."""
    durations = {
        _scoped_module(suffix): (40.0 if suffix == "aaa" else 10.0)
        for suffix in SCOPED_MODULE_SIZES
    }
    selections = [_part(scoped_inventory, index, 2, durations) for index in range(2)]
    seconds = [
        sum(durations[module] for module in {scope_of(nodeid) for nodeid in selected})
        for selected in selections
    ]
    assert max(seconds) / min(seconds) == pytest.approx(1.0)
    assert len(selections[0]) != len(selections[1]), (
        "a duration-balanced split of this record is necessarily count-imbalanced"
    )


@pytest.mark.unit
def test_part_assignment_falls_back_to_test_count_when_nothing_is_recorded(scoped_inventory):
    """A first run before any duration is recorded must still partition
    correctly -- just balanced by test count instead of measured seconds."""
    selections = [_part(scoped_inventory, index, 2) for index in range(2)]
    assert sum(len(selected) for selected in selections) == sum(SCOPED_MODULE_SIZES.values())
    assert len(selections[0]) == len(selections[1])


@pytest.mark.unit
def test_a_module_whose_tests_span_two_lanes_lands_wholly_in_one_part(tmp_path):
    """Scopes are assigned across every requested lane at once, so a module
    carrying both ``unit`` and ``browser`` tests is not split by the
    partition even though the two lanes execute separately."""
    snapshot = tmp_path / "snapshot"
    _write_snapshot_shell(snapshot, markers=("unit", "browser"))
    (snapshot / "tests" / "test_scope_mixed.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.unit\ndef test_unit_side(): assert True\n\n"
        "@pytest.mark.browser\ndef test_browser_side(): assert True\n"
    )
    for suffix in ("filler_one", "filler_two"):
        (snapshot / "tests" / f"test_scope_{suffix}.py").write_text(
            "import pytest\n\n@pytest.mark.unit\ndef test_case(): assert True\n"
        )
    inventory = _collect_snapshot(snapshot, tmp_path)
    lanes = ["fast-unit", "browser-free"]
    owners: dict[str, set[int]] = {}
    for index in range(2):
        selected = _expected(inventory, required_lanes=lanes, part=(index, 2), scope_durations={})
        for nodeids in selected.values():
            for nodeid in nodeids:
                owners.setdefault(scope_of(nodeid), set()).add(index)
    assert len(owners["tests/test_scope_mixed.py"]) == 1


@pytest.mark.unit
def test_a_lane_a_part_owns_nothing_of_is_omitted_rather_than_reported_empty(tmp_path):
    """A part that owns no module of a requested lane must not claim an empty
    run of it."""
    snapshot = tmp_path / "snapshot"
    _write_snapshot_shell(snapshot, markers=("unit", "browser"))
    (snapshot / "tests" / "test_scope_browser_only.py").write_text(
        "import pytest\n\n@pytest.mark.browser\ndef test_browser_side(): assert True\n"
    )
    (snapshot / "tests" / "test_scope_unit_only.py").write_text(
        "import pytest\n\n@pytest.mark.unit\ndef test_unit_side(): assert True\n"
    )
    inventory = _collect_snapshot(snapshot, tmp_path)
    lanes = ["fast-unit", "browser-free"]
    owned = [
        set(_expected(inventory, required_lanes=lanes, part=(index, 2), scope_durations={}))
        for index in range(2)
    ]
    assert sorted(owned, key=sorted) == [{"browser-free"}, {"fast-unit"}]


@pytest.mark.unit
def test_empty_part_fails_loudly_instead_of_a_vacuous_pass(scoped_inventory):
    """Requesting more parts than there are modules must fail closed for the
    parts that own nothing at all, never silently report a pass."""
    module_count = len(SCOPED_MODULE_SIZES)
    with pytest.raises(CandidateVerificationError, match="selected zero"):
        _part(scoped_inventory, module_count, module_count + 1)


@pytest.mark.unit
@pytest.mark.parametrize(
    "part_index, part_count",
    [(-1, 4), (4, 4), (5, 4), (0, 0), (0, -1), (2, 1)],
)
def test_out_of_range_part_arguments_are_rejected_before_any_collection(part_index, part_count):
    """Index/count validity depends only on the two integers, so it rejects
    before a snapshot, subprocess, or collection ever starts."""
    with pytest.raises(CandidateVerificationError):
        partition_lane_scopes({"fast-unit": ("tests/test_a.py::test_case",)}, part_index, part_count)
    with pytest.raises(CandidateVerificationError):
        _parse_part(part_index, part_count)


@pytest.mark.unit
def test_parse_part_requires_both_index_and_count_together():
    assert _parse_part(None, None) is None
    assert _parse_part(1, 4) == (1, 4)
    with pytest.raises(CandidateVerificationError):
        _parse_part(1, None)
    with pytest.raises(CandidateVerificationError):
        _parse_part(None, 4)


@pytest.mark.unit
def test_cli_rejects_an_out_of_range_part_before_touching_the_repository(tmp_path, capsys):
    """``tmp_path`` is not a git repository at all -- if part validation ran
    after repository access, this would fail with a git error instead."""
    rc = _main([
        "pushed-ref", "--repository", str(tmp_path), "--sha", "deadbeef",
        "--part-index", "9", "--part-count", "4",
    ])
    assert rc == 1
    assert "part" in capsys.readouterr().err.lower()


@pytest.mark.unit
def test_cli_rejects_requesting_a_shard_and_a_part_at_once(tmp_path, capsys):
    """Composing both slicings would select a set no other run reproduces."""
    rc = _main([
        "local", "--source", str(tmp_path),
        "--shard-index", "0", "--shard-count", "2",
        "--part-index", "0", "--part-count", "2",
    ])
    assert rc == 1
    assert "alternative partitions" in capsys.readouterr().err


@pytest.mark.unit
def test_absent_or_malformed_duration_record_reads_as_no_record(tmp_path):
    """Balance is a hint, never a correctness input: an unreadable record
    must degrade to the test-count estimate rather than fail a run."""
    assert load_scope_durations(tmp_path / "missing.json") == {}
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json")
    assert load_scope_durations(malformed) == {}
    wrong_shape = tmp_path / "wrong-shape.json"
    wrong_shape.write_text('["tests/test_a.py", 3]')
    assert load_scope_durations(wrong_shape) == {}
    mixed = tmp_path / "mixed.json"
    mixed.write_text('{"tests/test_a.py": 1.5, "tests/test_b.py": "slow", "tests/test_c.py": -2}')
    assert load_scope_durations(mixed) == {"tests/test_a.py": 1.5}


@pytest.mark.unit
def test_merge_scope_durations_sums_every_receipt_of_a_partitioned_run(tmp_path):
    """One record must cover a whole run however many lanes or parts it was
    spread over, so the next run's assignment sees every module."""
    first = tmp_path / "fast-unit.json"
    first.write_text(json.dumps({"scope_durations": {"tests/test_a.py": 2.5, "tests/test_b.py": 1.0}}))
    second = tmp_path / "browser-free.json"
    second.write_text(json.dumps({"scope_durations": {"tests/test_a.py": 0.5, "tests/test_c.py": 4.0}}))
    assert merge_scope_durations([first, second]) == {
        "tests/test_a.py": 3.0, "tests/test_b.py": 1.0, "tests/test_c.py": 4.0,
    }
    silent = tmp_path / "silent.json"
    silent.write_text(json.dumps({"reports": {}}))
    with pytest.raises(CandidateVerificationError, match="no scope durations"):
        merge_scope_durations([silent])


@pytest.mark.unit
def test_committed_duration_record_names_only_real_test_modules():
    """The record the gate balances against must stay a plain module-to-seconds
    map of this repository's own test modules, so a hand-edited entry cannot
    quietly steer the assignment toward some other part of the tree."""
    record = load_scope_durations(REPO / "scripts" / SCOPE_DURATION_RECORD)
    assert record, "the committed record must carry measured durations"
    for module in record:
        assert module.startswith("tests/") and module.endswith(".py"), module
    # A real measurement of a lane this size totals minutes of work; a
    # placeholder or all-zero record would not.
    assert sum(record.values()) > 60


def _paired_snapshot(tmp_path: Path) -> Path:
    """Three modules whose sorted node-ID order puts the boundary of a
    two-way *node-ID* split inside one module, and whose middle module has a
    test that skips unless its own module ran first in the same process.

    This is the repository's real conditional-skip shape in miniature: it is
    what makes an otherwise-green node-ID split report a skip the
    unpartitioned run does not, which the verifier then refuses.
    """
    snapshot = tmp_path / "snapshot"
    _write_snapshot_shell(snapshot)
    (snapshot / "tests" / "test_aaa_plain.py").write_text(
        "import pytest\n\n@pytest.mark.unit\ndef test_case(): assert True\n"
    )
    (snapshot / "tests" / "test_mid_paired.py").write_text(
        "import pytest\n\n"
        "_established = []\n\n"
        "@pytest.mark.unit\ndef test_1_establishes_module_state():\n"
        "    _established.append('ready')\n"
        "    assert _established\n\n"
        "@pytest.mark.unit\ndef test_2_requires_module_state():\n"
        "    if not _established:\n"
        "        pytest.skip('module prerequisite was not established in this process')\n"
        "    assert _established == ['ready']\n"
    )
    (snapshot / "tests" / "test_zzz_plain.py").write_text(
        "import pytest\n\n@pytest.mark.unit\ndef test_case(): assert True\n"
    )
    return snapshot


def _execute(snapshot: Path, workspace: Path, nodeids: Sequence[str], label: str) -> dict[str, str]:
    """Really run exactly ``nodeids`` through the shipped lane plugin and
    return its per-node outcome receipt -- the same receipt the verifier's
    completeness rule reads."""
    environment = make_hermetic_environment(workspace / f"{label}-runtime", workers=1)
    nodeid_file = workspace / f"{label}-nodeids.txt"
    nodeid_file.write_text("\n".join(nodeids) + "\n")
    receipt = workspace / f"{label}-execution.json"
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider",
            "-p", "scripts.test_lane_plugin",
            "--lifeos-lane-nodeids", str(nodeid_file),
            "--lifeos-lane-execution", str(receipt),
            "-m", marker(BY_NAME["fast-unit"]),
        ],
        cwd=snapshot, text=True, capture_output=True, env=environment,
    )
    assert receipt.is_file(), f"{result.stdout[-2000:]}{result.stderr[-2000:]}"
    return json.loads(receipt.read_text(encoding="utf-8"))["reports"]


def _skipped(reports: Mapping[str, str]) -> int:
    return sum(1 for outcome in reports.values() if outcome == "skipped")


@pytest.mark.unit
def test_a_scope_partition_reports_the_same_skip_count_as_an_unpartitioned_run(tmp_path):
    """The criterion that decides whether a partition may be trusted at all.

    A partitioned run must select the same tests *and* observe the same
    outcomes: the verifier requires every requested node ID to report
    ``passed``, so one extra conditional skip fails the lane even though the
    part's own pytest exits zero with everything it ran green. Proven by
    really executing the same collected inventory three ways and comparing
    total skips -- a node-ID split is included to show the difference is the
    partition unit, not the act of splitting.
    """
    snapshot = _paired_snapshot(tmp_path)
    inventory = _collect_snapshot(snapshot, tmp_path)
    whole = _expected(inventory, required_lanes=["fast-unit"])["fast-unit"]

    unpartitioned = _execute(snapshot, tmp_path, whole, "whole")
    assert set(unpartitioned) == set(whole)
    assert _skipped(unpartitioned) == 0

    parts = [_part(inventory, index, 2) for index in range(2)]
    sharded = [
        _expected(inventory, required_lanes=["fast-unit"], shard=(index, 2))["fast-unit"]
        for index in range(2)
    ]
    # Both splits must really be splits of the same whole, so the skip counts
    # below compare like with like.
    for split in (parts, sharded):
        assert set(split[0]) | set(split[1]) == set(whole)
        assert not set(split[0]) & set(split[1])
    # The node-ID split genuinely separates the paired module; the scope
    # partition genuinely keeps it whole. Without both, the comparison would
    # prove nothing about the unit.
    assert len({scope_of(nodeid) for nodeid in sharded[0]} & {scope_of(nodeid) for nodeid in sharded[1]}) == 1
    assert not {scope_of(nodeid) for nodeid in parts[0]} & {scope_of(nodeid) for nodeid in parts[1]}

    partitioned_skips = sum(
        _skipped(_execute(snapshot, tmp_path, selected, f"part-{index}"))
        for index, selected in enumerate(parts)
    )
    sharded_skips = sum(
        _skipped(_execute(snapshot, tmp_path, selected, f"shard-{index}"))
        for index, selected in enumerate(sharded)
    )

    assert partitioned_skips == _skipped(unpartitioned)
    assert sharded_skips > _skipped(unpartitioned)
