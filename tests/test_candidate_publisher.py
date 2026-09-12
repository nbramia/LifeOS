"""Tests for scripts/candidate_publisher.py.

The most safety-critical property here — that an explicit
``--force-with-lease`` on ``main`` catches a race a plain fast-forward check
would miss — is proven against a real local bare git repository, not
asserted. GitHub-calling functions (PR lookup, workflow dispatch, check
polling) are tested with an injected fake runner so their orchestration
logic (fork refusal, app-id filtering, retry/timeout behavior) is verified
without any network access.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import candidate_publisher as publisher

from scripts.candidate_publisher import (
    CandidateCommit,
    CheckOutcome,
    ForkPublicationRefusedError,
    MergeConflictError,
    PublisherError,
    SharedHeadPublicationRefusedError,
    build_candidate,
    cleanup_source_with_lease,
    fetch_pull_request,
    publish_atomic,
    publish_pull_request,
    await_required_check,
    trigger_trusted_verification,
    _closing_references,
)

pytestmark = pytest.mark.unit


def _init_bare_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "--initial-branch=main", str(bare)],
        check=True,
    )
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "Test"], check=True)
    return bare, clone


def _commit(clone: Path, filename: str, content: str, *, author: str | None = None, email: str | None = None) -> str:
    (clone / filename).write_text(content)
    subprocess.run(["git", "-C", str(clone), "add", filename], check=True)
    args = ["git", "-C", str(clone), "commit", "-q", "-m", f"add {filename}"]
    if author and email:
        args = [
            "git", "-C", str(clone),
            "-c", f"user.name={author}", "-c", f"user.email={email}",
            "commit", "-q", "-m", f"add {filename}", "--author", f"{author} <{email}>",
        ]
    subprocess.run(args, check=True)
    return subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def _push(clone: Path, refspec: str) -> None:
    subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", refspec], check=True)


# ---------------------------------------------------------------------------
# build_candidate: real three-way merge, attribution, conflict detection
# ---------------------------------------------------------------------------

def test_build_candidate_merges_base_only_and_head_only_changes(tmp_path):
    _, clone = _init_bare_and_clone(tmp_path)
    _commit(clone, "a.txt", "a")

    # Head-only change on a branch from base0.
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "-b", "feature"], check=True)
    head = _commit(clone, "b.txt", "b", author="Alex Chen", email="alex@example.com")

    # Advance the base independently after creating the feature branch.
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "main"], check=True)
    base1 = _commit(clone, "c.txt", "c")

    candidate = build_candidate(clone, base1, head, pr_number=1, pr_title="feat: add b and c", closes_text="Closes #9")

    assert candidate.base_sha == base1
    assert candidate.head_sha == head
    assert candidate.author_name == "Alex Chen"
    assert candidate.author_email == "alex@example.com"
    assert candidate.normalized_date.endswith("T12:00:00+00:00")

    # The candidate's tree must contain ALL THREE files — base-only (c.txt),
    # head-only (b.txt), and the original (a.txt) — proving a real merge,
    # not a copy of either side's tree alone.
    listing = subprocess.run(
        ["git", "-C", str(clone), "ls-tree", "-r", "--name-only", candidate.sha],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    assert set(listing) == {"a.txt", "b.txt", "c.txt"}

    parents = subprocess.run(
        ["git", "-C", str(clone), "log", "-1", "--format=%P", candidate.sha],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    assert parents == [base1, head]

    # No leftover merge worktree.
    worktrees = subprocess.run(["git", "-C", str(clone), "worktree", "list"], check=True, capture_output=True, text=True).stdout
    assert "lifeos-candidate-merge" not in worktrees


def test_build_candidate_refuses_to_guess_a_conflict_resolution(tmp_path):
    _, clone = _init_bare_and_clone(tmp_path)
    _commit(clone, "shared.txt", "original")
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "-b", "feature"], check=True)
    head = _commit(clone, "shared.txt", "head version")
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "main"], check=True)
    base1 = _commit(clone, "shared.txt", "conflicting base version")

    with pytest.raises(MergeConflictError):
        build_candidate(clone, base1, head, pr_number=2, pr_title="feat: conflicting change")

    # A refused merge must not leave a stray worktree or half-finished state.
    worktrees = subprocess.run(["git", "-C", str(clone), "worktree", "list"], check=True, capture_output=True, text=True).stdout
    assert "lifeos-candidate-merge" not in worktrees


# ---------------------------------------------------------------------------
# publish_atomic: the core safety property — explicit lease vs plain
# fast-forward for the MAIN ref, proven against a real bare repo.
# ---------------------------------------------------------------------------

def test_publish_atomic_accepts_the_expected_case(tmp_path):
    _, clone = _init_bare_and_clone(tmp_path)
    base = _commit(clone, "a.txt", "a")
    _push(clone, "main")
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "-b", "feature"], check=True)
    head = _commit(clone, "b.txt", "b")

    candidate = build_candidate(clone, base, head, pr_number=1, pr_title="feat: add b")
    outcome = publish_atomic(
        clone, "origin", candidate.sha, main_ref="refs/heads/main", expected_main_sha=base,
    )
    assert outcome.accepted
    remote_tip = subprocess.run(["git", "-C", str(clone), "ls-remote", "origin", "refs/heads/main"], check=True, capture_output=True, text=True).stdout.split()[0]
    assert remote_tip == candidate.sha


def test_publish_atomic_rejects_divergent_main_race(tmp_path):
    bare, clone = _init_bare_and_clone(tmp_path)
    base = _commit(clone, "a.txt", "a")
    _push(clone, "main")

    # A second, unrelated commit lands on main after we "observed" base.
    racer = _commit(clone, "unrelated.txt", "x")
    _push(clone, "main")

    # Build a candidate whose parent is the STALE base, then try to publish
    # it as if `base` were still main's tip.
    candidate = subprocess.run(
        ["git", "-C", str(clone), "commit-tree", f"{base}^{{tree}}", "-p", base, "-m", "candidate"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    outcome = publish_atomic(clone, "origin", candidate, main_ref="refs/heads/main", expected_main_sha=base)
    assert not outcome.accepted
    remote_tip = subprocess.run(["git", "-C", str(clone), "ls-remote", "origin", "refs/heads/main"], check=True, capture_output=True, text=True).stdout.split()[0]
    assert remote_tip == racer, "the racer's commit must survive a rejected publish attempt"


def test_publish_atomic_refuses_locally_when_expected_main_is_not_an_ancestor(tmp_path):
    """The lease authorizes only a fast-forward candidate whose ancestry
    includes expected_main_sha; publish_atomic rejects other candidates
    locally before pushing."""
    _, clone = _init_bare_and_clone(tmp_path)
    base = _commit(clone, "a.txt", "a")
    _push(clone, "main")

    # A genuinely disjoint history (orphan branch) — not an ancestor of
    # `base` in either direction.
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "--orphan", "unrelated-branch"], check=True)
    subprocess.run(["git", "-C", str(clone), "rm", "-rf", "-q", "."], check=True, cwd=str(clone))
    unrelated = _commit(clone, "unrelated.txt", "x")

    with pytest.raises(PublisherError, match="not an ancestor"):
        publish_atomic(clone, "origin", base, main_ref="refs/heads/main", expected_main_sha=unrelated)


def test_publish_atomic_lease_catches_main_reset_to_an_ancestor_that_plain_fast_forward_would_accept(tmp_path):
    """This is the exact gap flagged for this implementation: a plain
    non-force push only checks "is the new value a descendant of whatever
    is on the ref right now" — it does NOT check "is the ref still at the
    exact SHA I observed." If main's tip is reset BACKWARD to a commit that
    is *also* an ancestor of our candidate (an admin reset, a corrupted
    ref, etc.), a plain fast-forward push would silently succeed even
    though main is not what was actually observed. An explicit
    --force-with-lease=<ref>:<expected-sha> must reject this, because it
    compares the CURRENT value against the exact expected value, not merely
    "would this be a fast-forward."
    """
    bare, clone = _init_bare_and_clone(tmp_path)
    root = _commit(clone, "root.txt", "root")
    base = _commit(clone, "a.txt", "a")  # main is now root -> base
    _push(clone, "main")

    # Candidate is built with `base` as its first parent (root -> base -> candidate).
    candidate = subprocess.run(
        ["git", "-C", str(clone), "commit-tree", f"{base}^{{tree}}", "-p", base, "-m", "candidate"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    # Main gets reset BACKWARD to `root` — an ancestor of `base`, which is
    # itself an ancestor of `candidate`. `candidate` is therefore STILL a
    # valid fast-forward of the new (reset) tip, even though main is no
    # longer at the SHA (`base`) this publish was supposed to be contingent on.
    subprocess.run(["git", "-C", str(clone), "push", "-q", "--force", "origin", f"{root}:refs/heads/main"], check=True)

    # Sanity check: a PLAIN non-force push of `candidate` onto this reset
    # main WOULD be accepted, because `candidate` is a descendant of `root`.
    # This demonstrates the exact gap a fast-forward-only check has.
    plain_push = subprocess.run(
        ["git", "-C", str(clone), "push", "origin", f"{candidate}:refs/heads/main"],
        capture_output=True, text=True,
    )
    assert plain_push.returncode == 0, (
        "expected the plain (non-lease) push to succeed here, demonstrating the gap"
    )
    # Reset the remote back to `root` so the real (leased) assertion below
    # starts from the same "main was reset backward" precondition.
    subprocess.run(["git", "-C", str(clone), "push", "-q", "--force", "origin", f"{root}:refs/heads/main"], check=True)

    # Now prove publish_atomic's EXPLICIT lease rejects exactly this case,
    # where the plain push above did not.
    outcome = publish_atomic(clone, "origin", candidate, main_ref="refs/heads/main", expected_main_sha=base)
    assert not outcome.accepted, "an explicit lease must reject a main that was reset to an ancestor of the candidate"
    remote_tip = subprocess.run(["git", "-C", str(clone), "ls-remote", "origin", "refs/heads/main"], check=True, capture_output=True, text=True).stdout.split()[0]
    assert remote_tip == root, "the reset value must survive a correctly-rejected leased publish"


def test_publish_atomic_rejects_stale_source_lease_and_publishes_both_refs_on_success(tmp_path):
    bare, clone = _init_bare_and_clone(tmp_path)
    base = _commit(clone, "a.txt", "a")
    _push(clone, "main")
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "-b", "feature"], check=True)
    head = _commit(clone, "b.txt", "b")
    _push(clone, "feature")

    candidate = build_candidate(clone, base, head, pr_number=3, pr_title="feat: add b")

    # Source ref advances after we observed `head`.
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "feature"], check=True)
    newer_head = _commit(clone, "c.txt", "c")
    _push(clone, "feature")
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "main"], check=True)

    stale_outcome = publish_atomic(
        clone, "origin", candidate.sha, main_ref="refs/heads/main", expected_main_sha=base,
        source_ref="refs/heads/feature", expected_source_sha=head, delete_source=True,
    )
    assert not stale_outcome.accepted
    remote_feature = subprocess.run(["git", "-C", str(clone), "ls-remote", "origin", "refs/heads/feature"], check=True, capture_output=True, text=True).stdout.split()[0]
    assert remote_feature == newer_head, "the newer source commit must survive a rejected transaction"
    remote_main = subprocess.run(["git", "-C", str(clone), "ls-remote", "origin", "refs/heads/main"], check=True, capture_output=True, text=True).stdout.split()[0]
    assert remote_main == base, "main must be untouched when the paired source lease fails"

    # Now with the correct (fresh) source lease, the whole transaction succeeds.
    success_outcome = publish_atomic(
        clone, "origin", candidate.sha, main_ref="refs/heads/main", expected_main_sha=base,
        source_ref="refs/heads/feature", expected_source_sha=newer_head, delete_source=True,
    )
    assert success_outcome.accepted
    remote_main2 = subprocess.run(["git", "-C", str(clone), "ls-remote", "origin", "refs/heads/main"], check=True, capture_output=True, text=True).stdout.split()[0]
    assert remote_main2 == candidate.sha
    remote_refs = subprocess.run(["git", "-C", str(clone), "ls-remote", "origin"], check=True, capture_output=True, text=True).stdout
    assert "refs/heads/feature" not in remote_refs


def test_cleanup_source_with_lease_retains_newer_source_on_race(tmp_path):
    bare, clone = _init_bare_and_clone(tmp_path)
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "-b", "feature"], check=True)
    old_head = _commit(clone, "a.txt", "a")
    _push(clone, "feature")
    newer_head = _commit(clone, "b.txt", "b")
    _push(clone, "feature")

    outcome = cleanup_source_with_lease(clone, "origin", "refs/heads/feature", old_head)
    assert not outcome.accepted
    remote_feature = subprocess.run(["git", "-C", str(clone), "ls-remote", "origin", "refs/heads/feature"], check=True, capture_output=True, text=True).stdout.split()[0]
    assert remote_feature == newer_head


def test_publish_atomic_refuses_a_non_fast_forward_source_update_before_push(tmp_path):
    """A source lease is a CAS for a forward update, never a rewrite flag."""
    _, clone = _init_bare_and_clone(tmp_path)
    base = _commit(clone, "a.txt", "a")
    _push(clone, "main")
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "-b", "feature"], check=True)
    head = _commit(clone, "feature.txt", "feature")
    _push(clone, "feature")
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "main"], check=True)
    candidate = subprocess.run(
        ["git", "-C", str(clone), "commit-tree", f"{base}^{{tree}}", "-p", base, "-m", "candidate"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    with pytest.raises(PublisherError, match="expected source.*not an ancestor"):
        publish_atomic(
            clone, "origin", candidate, main_ref="refs/heads/main", expected_main_sha=base,
            source_ref="refs/heads/feature", expected_source_sha=head,
        )
    remote_main = subprocess.run(
        ["git", "-C", str(clone), "ls-remote", "origin", "refs/heads/main"],
        check=True, capture_output=True, text=True,
    ).stdout.split()[0]
    assert remote_main == base


# ---------------------------------------------------------------------------
# fetch_pull_request: fork refusal (no network — injected runner)
# ---------------------------------------------------------------------------

def _fake_run(stdout_obj, returncode=0):
    def run(args, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(stdout_obj) if isinstance(stdout_obj, (dict, list)) else stdout_obj, stderr="")
    return run


def test_fetch_pull_request_refuses_cross_repository_fork():
    payload = {
        "number": 5, "baseRefName": "main", "headRefOid": "abc123",
        "headRepository": {"name": "LifeOS"}, "headRepositoryOwner": {"login": "someone-else"},
        "isCrossRepository": True, "title": "t", "body": "",
    }
    with pytest.raises(ForkPublicationRefusedError):
        fetch_pull_request("nbramia/LifeOS", 5, run=_fake_run(payload))


def test_fetch_pull_request_accepts_same_repository_pr():
    payload = {
        "number": 5, "baseRefName": "main", "headRefOid": "abc123",
        "headRepository": {"name": "LifeOS"}, "headRepositoryOwner": {"login": "nbramia"},
        "isCrossRepository": False, "title": "t", "body": "Closes #1",
    }
    pr = fetch_pull_request("nbramia/LifeOS", 5, run=_fake_run(payload))
    assert pr.head_sha == "abc123" and not pr.is_cross_repository


# ---------------------------------------------------------------------------
# trigger_trusted_verification: dispatch must carry PR identity too
# ---------------------------------------------------------------------------

def test_trigger_trusted_verification_passes_pr_number_alongside_candidate_sha():
    """A superseded candidate for the same PR must be cancellable by the
    workflow's own concurrency group, which is keyed on PR number — the
    dispatch call must actually carry that input, not just candidate_sha."""
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    trusted_runner_sha = "a" * 40
    trigger_trusted_verification(
        "nbramia/LifeOS", "candidate-verification.yml", "deadbeef",
        pr_number=7, trusted_runner_sha=trusted_runner_sha, run=run,
    )
    assert calls == [[
        "gh", "workflow", "run", "candidate-verification.yml", "--repo", "nbramia/LifeOS", "--ref", "main",
        "-f", "candidate_sha=deadbeef", "-f", "pr_number=7",
        "-f", f"trusted_runner_sha={trusted_runner_sha}",
    ]]


# ---------------------------------------------------------------------------
# await_required_check: app-id scoping, no real network/sleep
# ---------------------------------------------------------------------------

def test_await_required_check_ignores_a_matching_name_from_the_wrong_app():
    """The exact property that closes the forged-context vulnerability:
    a check with the right NAME but the wrong reporting app must never
    satisfy the wait."""
    calls = {"n": 0}

    def run(args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            payload = {"check_runs": [{"name": "candidate-verification", "status": "completed",
                                        "conclusion": "success", "app": {"id": 999999}}]}
        else:
            payload = {"check_runs": [{"name": "candidate-verification", "status": "completed",
                                        "conclusion": "success", "app": {"id": 42}}]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    sleeps = []
    outcome = await_required_check(
        "nbramia/LifeOS", "deadbeef", "candidate-verification", trusted_app_id=42,
        timeout_seconds=100, poll_interval_seconds=1, run=run,
        sleep=lambda s: sleeps.append(s), now=iter([0, 1, 2]).__next__,
    )
    assert outcome.state == "success"
    assert calls["n"] == 2
    assert sleeps == [1]


def _check_run(run_id: int, conclusion: str, *, started_at: str, app_id: int = 42,
               name: str = "candidate-verification") -> dict:
    return {
        "id": run_id, "name": name, "status": "completed", "conclusion": conclusion,
        "started_at": started_at, "completed_at": started_at, "app": {"id": app_id},
    }


def test_await_required_check_ignores_a_verdict_that_predates_this_dispatch():
    """A candidate SHA is deterministic from its content, so re-publishing an
    unchanged branch lands on a SHA that may already carry an earlier verdict.
    A stale failure must not refuse a candidate whose fresh run passed."""
    payload = {"check_runs": [
        _check_run(1, "failure", started_at="2026-01-01T00:00:00Z"),
        _check_run(2, "success", started_at="2026-01-01T01:00:00Z"),
    ]}

    def run(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    outcome = await_required_check(
        "nbramia/LifeOS", "deadbeef", "candidate-verification", trusted_app_id=42,
        ignore_check_run_ids={1},
        timeout_seconds=100, poll_interval_seconds=1, run=run,
        sleep=lambda s: None, now=iter([0, 1]).__next__,
    )
    assert outcome.state == "success"


def test_await_required_check_never_publishes_on_a_stale_success():
    """The graver direction: a success left over from an earlier dispatch must
    not authorize publication of a candidate whose own run failed."""
    payload = {"check_runs": [
        _check_run(7, "success", started_at="2026-01-01T00:00:00Z"),
        _check_run(8, "failure", started_at="2026-01-01T01:00:00Z"),
    ]}

    def run(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    outcome = await_required_check(
        "nbramia/LifeOS", "deadbeef", "candidate-verification", trusted_app_id=42,
        ignore_check_run_ids={7},
        timeout_seconds=100, poll_interval_seconds=1, run=run,
        sleep=lambda s: None, now=iter([0, 1]).__next__,
    )
    assert outcome.state == "failure"


def test_await_required_check_waits_rather_than_reading_an_ignored_verdict():
    """With only the ignored verdict present, the wait keeps polling — it does
    not fall back to the stale answer, and it does not report success."""
    calls = {"n": 0}
    stale_only = {"check_runs": [_check_run(1, "success", started_at="2026-01-01T00:00:00Z")]}

    def run(args, **kwargs):
        calls["n"] += 1
        return SimpleNamespace(returncode=0, stdout=json.dumps(stale_only), stderr="")

    clock = iter([0, 50, 150])
    outcome = await_required_check(
        "nbramia/LifeOS", "deadbeef", "candidate-verification", trusted_app_id=42,
        ignore_check_run_ids={1},
        timeout_seconds=100, poll_interval_seconds=1, run=run,
        sleep=lambda s: None, now=lambda: next(clock),
    )
    assert outcome.state == "timeout"
    assert calls["n"] >= 2


def test_await_required_check_reads_the_newest_completed_check_not_the_first_listed():
    """Ordering in the API response must not decide the verdict."""
    payload = {"check_runs": [
        _check_run(3, "failure", started_at="2026-01-01T00:00:00Z"),
        _check_run(4, "success", started_at="2026-01-01T02:00:00Z"),
    ]}

    def run(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    outcome = await_required_check(
        "nbramia/LifeOS", "deadbeef", "candidate-verification", trusted_app_id=42,
        timeout_seconds=100, poll_interval_seconds=1, run=run,
        sleep=lambda s: None, now=iter([0, 1]).__next__,
    )
    assert outcome.state == "success"


def test_existing_check_run_ids_snapshots_only_the_trusted_issuer():
    payload = {"check_runs": [
        _check_run(1, "failure", started_at="2026-01-01T00:00:00Z"),
        _check_run(2, "success", started_at="2026-01-01T00:00:00Z", app_id=999999),
        _check_run(3, "success", started_at="2026-01-01T00:00:00Z", name="something-else"),
    ]}

    def run(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    assert publisher.existing_check_run_ids(
        "nbramia/LifeOS", "deadbeef", "candidate-verification", trusted_app_id=42, run=run,
    ) == frozenset({1})


def test_existing_check_run_ids_raises_rather_than_reporting_none_on_a_failed_query():
    """"No existing checks" and "could not tell" must not collapse into the
    same value — the empty set is exactly what would let a stale verdict
    through."""
    def run(args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    with pytest.raises(publisher.PublisherError, match="could not read existing"):
        publisher.existing_check_run_ids(
            "nbramia/LifeOS", "deadbeef", "candidate-verification", trusted_app_id=42, run=run,
        )


def test_await_required_check_times_out_when_nothing_matches():
    def run(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"check_runs": []}), stderr="")

    clock = iter([0, 50, 150])
    outcome = await_required_check(
        "nbramia/LifeOS", "deadbeef", "candidate-verification", trusted_app_id=42,
        timeout_seconds=100, poll_interval_seconds=1, run=run,
        sleep=lambda s: None, now=lambda: next(clock),
    )
    assert outcome.state == "timeout"


# ---------------------------------------------------------------------------
# _closing_references: PR-body keyword extraction for the merge commit
# ---------------------------------------------------------------------------

def test_closing_references_extracts_and_dedupes_keywords():
    """Every closing synonym GitHub recognizes normalizes onto its own
    "Closes #N" line — GitHub treats close/fix/resolve as equivalent, so a
    single normalized keyword is a legitimate simplification, not a loss of
    information."""
    body = "This closes #42 and also Fixes #42 again.\n\nResolves #7."
    assert _closing_references(body) == "Closes #42\nCloses #7"


def test_closing_references_empty_when_no_keyword_present():
    assert _closing_references("Just a description, see #42 for context.") == ""


# ---------------------------------------------------------------------------
# publish_pull_request: full orchestration, real git + a faked `gh`
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("delete_source", [True, False], ids=["delete-source", "retain-source"])
def test_publish_pull_request_runs_construct_then_verify_then_publish_in_order(tmp_path, delete_source):
    """Real git operations end to end (fetch, merge, staging push, atomic
    publish, source+staging cleanup); only `gh` calls are faked, proving the
    orchestration wires every step in the required order without touching
    the network."""
    _, clone = _init_bare_and_clone(tmp_path)
    _commit(clone, "a.txt", "a")
    _push(clone, "main")

    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "-b", "feature"], check=True)
    head = _commit(clone, "b.txt", "b", author="Alex Chen", email="alex@example.com")
    _push(clone, "feature")
    # Simulate GitHub's own always-present `refs/pull/<n>/head` ref.
    subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", f"{head}:refs/pull/7/head"], check=True)
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "main"], check=True)

    pr_payload = {
        "number": 7, "baseRefName": "main", "headRefName": "feature", "headRefOid": head,
        "headRepository": {"name": "LifeOS"}, "headRepositoryOwner": {"login": "nbramia"},
        "isCrossRepository": False, "title": "feat: add b", "body": "Closes #42",
    }
    gh_calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        if args[0] == "gh":
            gh_calls.append(args)
            if args[1:3] == ["pr", "view"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps(pr_payload), stderr="")
            if args[1] == "api" and args[-1].startswith("repos/nbramia/LifeOS/pulls?"):
                return SimpleNamespace(returncode=0, stdout=json.dumps([[_head_row(
                    7, "nbramia/LifeOS", "feature",
                )]]), stderr="")
            if args[1:3] == ["workflow", "run"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[1] == "api":
                payload = {"check_runs": [{
                    "name": "candidate-verify", "status": "completed", "conclusion": "success",
                    "app": {"id": 999}, "completed_at": "now",
                }]}
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
            raise AssertionError(f"unexpected gh call: {args}")
        return subprocess.run(args, **kwargs)

    result = publish_pull_request(
        "nbramia/LifeOS", 7,
        repo=clone, remote="origin",
        workflow_file="candidate-verification.yml", check_name="candidate-verify",
        trusted_app_id=999, delete_source=delete_source,
        run=fake_run, sleep=lambda s: None, now=lambda: 0.0,
    )

    assert result.publish.accepted
    if delete_source:
        assert result.cleanup is not None and result.cleanup.accepted
    else:
        assert result.cleanup is None
    assert "Closes #42" in subprocess.run(
        ["git", "-C", str(clone), "log", "-1", "--format=%B", result.candidate.sha],
        check=True, capture_output=True, text=True,
    ).stdout

    remote_refs = subprocess.run(
        ["git", "-C", str(clone), "ls-remote", "origin"], check=True, capture_output=True, text=True,
    ).stdout
    assert f"{result.candidate.sha}\trefs/heads/main" in remote_refs
    if delete_source:
        assert "refs/heads/feature" not in remote_refs, "source branch must be deleted after a successful publish"
    else:
        assert f"{result.candidate.sha}\trefs/heads/feature" in remote_refs, (
            "--no-delete-source retains the source ref at the exact published candidate"
        )
    assert "lifeos-candidate" not in remote_refs, "the internal staging ref must always be cleaned up"

    # gh performs only the pull-request lookup, trusted dispatch, and check poll;
    # publication uses Git.
    call_kinds = [(c[1], c[2] if len(c) > 2 else None) for c in gh_calls]
    assert ("pr", "view") in call_kinds
    assert ("workflow", "run") in call_kinds
    assert any(c[1] == "api" for c in gh_calls)
    assert not any(c[1:3] == ["pr", "merge"] for c in gh_calls), "must never call gh pr merge"


def test_publish_pull_request_rejects_whole_transaction_when_source_moves_during_verification(tmp_path):
    """Main and source advance atomically; a source commit gained during
    isolated verification makes the candidate stale, so the whole publish is
    rejected and both refs remain unchanged."""
    _, clone = _init_bare_and_clone(tmp_path)
    base = _commit(clone, "a.txt", "a")
    _push(clone, "main")

    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "-b", "feature"], check=True)
    head = _commit(clone, "b.txt", "b", author="Alex Chen", email="alex@example.com")
    _push(clone, "feature")
    subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", f"{head}:refs/pull/13/head"], check=True)
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "main"], check=True)

    pr_payload = {
        "number": 13, "baseRefName": "main", "headRefName": "feature", "headRefOid": head,
        "headRepository": {"name": "LifeOS"}, "headRepositoryOwner": {"login": "nbramia"},
        "isCrossRepository": False, "title": "feat: add b", "body": "",
    }

    def fake_run(args, **kwargs):
        if args[0] == "gh":
            if args[1:3] == ["pr", "view"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps(pr_payload), stderr="")
            if args[1] == "api" and args[-1].startswith("repos/nbramia/LifeOS/pulls?"):
                return SimpleNamespace(returncode=0, stdout=json.dumps([[_head_row(
                    13, "nbramia/LifeOS", "feature",
                )]]), stderr="")
            if args[1:3] == ["workflow", "run"]:
                # The source branch races ahead precisely during the window
                # verification would be running in production.
                subprocess.run(["git", "-C", str(clone), "checkout", "-q", "feature"], check=True)
                _commit(clone, "race.txt", "race")
                _push(clone, "feature")
                subprocess.run(["git", "-C", str(clone), "checkout", "-q", "main"], check=True)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[1] == "api":
                payload = {"check_runs": [{
                    "name": "candidate-verify", "status": "completed", "conclusion": "success",
                    "app": {"id": 999}, "completed_at": "now",
                }]}
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
            raise AssertionError(f"unexpected gh call: {args}")
        return subprocess.run(args, **kwargs)

    result = publish_pull_request(
        "nbramia/LifeOS", 13,
        repo=clone, remote="origin", trusted_app_id=999,
        workflow_file="candidate-verification.yml", check_name="candidate-verify",
        run=fake_run, sleep=lambda s: None, now=lambda: 0.0,
    )

    assert not result.publish.accepted
    assert result.cleanup is None, "cleanup must never run when the atomic publish itself was rejected"

    remote_refs = subprocess.run(
        ["git", "-C", str(clone), "ls-remote", "origin"], check=True, capture_output=True, text=True,
    ).stdout
    remote_main = [line for line in remote_refs.splitlines() if line.endswith("refs/heads/main")][0].split()[0]
    remote_feature = [line for line in remote_refs.splitlines() if line.endswith("refs/heads/feature")][0].split()[0]
    assert remote_main == base, "main must be untouched when the paired source lease fails"
    assert remote_feature != head, "the source branch must show its newer (raced) commit, not be rolled back"
    assert "lifeos-candidate" not in remote_refs, "the internal staging ref must still be cleaned up on rejection"


def test_publish_pull_request_refuses_fork_before_any_git_operation(tmp_path):
    _, clone = _init_bare_and_clone(tmp_path)
    _commit(clone, "a.txt", "a")
    _push(clone, "main")

    pr_payload = {
        "number": 9, "baseRefName": "main", "headRefName": "feature", "headRefOid": "deadbeef",
        "headRepository": {"name": "LifeOS"}, "headRepositoryOwner": {"login": "someone-else"},
        "isCrossRepository": True, "title": "t", "body": "",
    }
    git_calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        if args[0] == "gh":
            if args[1:3] == ["pr", "list"]:
                return SimpleNamespace(returncode=0, stdout="[]", stderr="")
            return SimpleNamespace(returncode=0, stdout=json.dumps(pr_payload), stderr="")
        git_calls.append(args)
        return subprocess.run(args, **kwargs)

    with pytest.raises(ForkPublicationRefusedError):
        publish_pull_request(
            "nbramia/LifeOS", 9,
            repo=clone, remote="origin", trusted_app_id=999,
            workflow_file="candidate-verification.yml", check_name="candidate-verify",
            run=fake_run,
        )
    assert not git_calls, "a fork must be refused before any git fetch/build/push is attempted"


def test_publish_pull_request_refuses_when_head_moved_since_expected_sha(tmp_path):
    """A caller that already validated some other evidence against a
    specific head SHA must not have a commit pushed in the gap since then
    silently substituted in and published instead."""
    _, clone = _init_bare_and_clone(tmp_path)
    _commit(clone, "a.txt", "a")
    _push(clone, "main")

    pr_payload = {
        "number": 11, "baseRefName": "main", "headRefName": "feature", "headRefOid": "freshsha",
        "headRepository": {"name": "LifeOS"}, "headRepositoryOwner": {"login": "nbramia"},
        "isCrossRepository": False, "title": "t", "body": "",
    }
    git_calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        if args[0] == "gh":
            return SimpleNamespace(returncode=0, stdout=json.dumps(pr_payload), stderr="")
        git_calls.append(args)
        return subprocess.run(args, **kwargs)

    with pytest.raises(PublisherError, match="not the one validated"):
        publish_pull_request(
            "nbramia/LifeOS", 11,
            repo=clone, remote="origin", trusted_app_id=999,
            workflow_file="candidate-verification.yml", check_name="candidate-verify",
            expected_head_sha="stalesha", run=fake_run,
        )
    assert not git_calls, "the stale-head refusal must happen before any git fetch/build/push"


@pytest.mark.parametrize("issuer", [15368, -1, 0, -7])
def test_production_publish_rejects_untrusted_issuer_before_github_lookup(tmp_path, issuer):
    """Generic Actions, wildcard, and nonpositive ids cannot reach PR lookup
    or any local fetch/push operation; disposable helper probes do not weaken
    this production entry-point boundary."""
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    with pytest.raises(PublisherError, match="dedicated|generic Actions|positive"):
        publish_pull_request(
            "nbramia/LifeOS", 17, repo=tmp_path, trusted_app_id=issuer,
            workflow_file="candidate-verification.yml", check_name="candidate-verify",
            run=fake_run,
        )
    assert calls == []


def test_open_shared_head_ref_is_refused_before_fetch(tmp_path):
    pr_payload = {
        "number": 21, "baseRefName": "main", "headRefName": "feature", "headRefOid": "headsha",
        "headRepository": {"name": "LifeOS"}, "headRepositoryOwner": {"login": "nbramia"},
        "isCrossRepository": False, "title": "t", "body": "",
    }
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(pr_payload), stderr="")
        if args[:2] == ["gh", "api"]:
            assert args[2:4] == ["--paginate", "--slurp"]
            assert args[-1] == "repos/nbramia/LifeOS/pulls?state=open&head=nbramia%3Afeature&per_page=100"
            return SimpleNamespace(returncode=0, stdout=json.dumps([[{
                "number": 21, "head": {"ref": "feature", "repo": {"full_name": "nbramia/LifeOS"}},
            }, {
                "number": 22, "headRefName": "feature",
                "head": {"ref": "feature", "repo": {"full_name": "nbramia/LifeOS"}},
            }]]), stderr="")
        raise AssertionError(args)

    with pytest.raises(SharedHeadPublicationRefusedError, match="#22"):
        publish_pull_request(
            "nbramia/LifeOS", 21, repo=tmp_path, trusted_app_id=999,
            workflow_file="candidate-verification.yml", check_name="candidate-verify",
            run=fake_run,
        )
    assert not any(args[:2] == ["git", "-C"] for args in calls)


def test_open_shared_head_ref_is_rechecked_before_atomic_publish(monkeypatch, tmp_path):
    """The final point-in-time API check must happen after verification and
    prevent the publish if another PR appears during that window."""
    pr = publisher.PullRequestRef(
        number=31, base_ref="main", base_repository="nbramia/LifeOS", head_sha="headsha",
        head_ref="feature", head_repository="nbramia/LifeOS", is_cross_repository=False,
        title="t", body="",
    )
    candidate = CandidateCommit(
        sha="candidate", tree="tree", base_sha="base", head_sha="headsha",
        author_name="A", author_email="a@example.com", normalized_date="2026-01-01T12:00:00Z",
    )
    monkeypatch.setattr(publisher, "fetch_pull_request", lambda *args, **kwargs: pr)
    monkeypatch.setattr(publisher, "_git", lambda *args, **kwargs: "base")
    monkeypatch.setattr(publisher, "build_candidate", lambda *args, **kwargs: candidate)
    monkeypatch.setattr(publisher, "staging_ref_for", lambda *args, **kwargs: "refs/heads/staging")
    monkeypatch.setattr(publisher, "push_candidate_for_verification", lambda *args, **kwargs: None)
    monkeypatch.setattr(publisher, "trigger_trusted_verification", lambda *args, **kwargs: None)
    monkeypatch.setattr(publisher, "await_required_check", lambda *args, **kwargs: CheckOutcome("success", "ok"))
    publish_calls: list[object] = []
    monkeypatch.setattr(publisher, "publish_atomic", lambda *args, **kwargs: publish_calls.append(args))
    list_calls = 0

    def fake_run(args, **kwargs):
        nonlocal list_calls
        if args[:2] == ["gh", "api"] and str(args[-1]).endswith("/check-runs"):
            # The pre-dispatch snapshot of verdicts already on this candidate;
            # counted separately from the head-exclusivity listings below.
            return SimpleNamespace(returncode=0, stdout=json.dumps({"check_runs": []}), stderr="")
        if args[:2] == ["gh", "api"]:
            list_calls += 1
            rows = [{
                "number": 31, "head": {"ref": "feature", "repo": {"full_name": "nbramia/LifeOS"}},
            }]
            if list_calls == 2:
                rows.append({
                    "number": 32, "head": {"ref": "feature", "repo": {"full_name": "nbramia/LifeOS"}},
                })
            return SimpleNamespace(returncode=0, stdout=json.dumps([rows]), stderr="")
        if args[:2] == ["git", "-C"] and "--delete" in args:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(args)

    with pytest.raises(SharedHeadPublicationRefusedError, match="#32"):
        publish_pull_request(
            "nbramia/LifeOS", 31, repo=tmp_path, trusted_app_id=999,
            workflow_file="candidate-verification.yml", check_name="candidate-verify",
            run=fake_run,
        )
    assert list_calls == 2
    assert not publish_calls


def _head_row(number: int, repository: str, ref: str) -> dict:
    return {"number": number, "head": {"ref": ref, "repo": {"full_name": repository}}}


def _same_repo_pr() -> publisher.PullRequestRef:
    return publisher.PullRequestRef(
        number=31, base_ref="main", base_repository="nbramia/LifeOS", head_sha="headsha",
        head_ref="feature", head_repository="nbramia/LifeOS", is_cross_repository=False,
        title="t", body="",
    )


def test_head_query_empty_result_fails_closed_instead_of_claiming_exclusivity():
    with pytest.raises(PublisherError, match="did not appear in its own paginated head query"):
        publisher._sharing_open_pull_requests(
            "nbramia/LifeOS", _same_repo_pr(),
            run=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="[]", stderr=""),
        )


def test_head_query_is_paginated_and_finds_a_conflict_beyond_the_default_30():
    pr = _same_repo_pr()
    fork_rows = [_head_row(number, "other/LifeOS", "feature") for number in range(1, 31)]
    pages = [[_head_row(pr.number, pr.head_repository, pr.head_ref), *fork_rows], [
        _head_row(99, pr.head_repository, pr.head_ref),
    ]]
    seen: list[list[str]] = []

    def run(args, **_kwargs):
        seen.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps(pages), stderr="")

    assert publisher._sharing_open_pull_requests("nbramia/LifeOS", pr, run=run) == (99,)
    assert seen[0][2:4] == ["--paginate", "--slurp"]
    assert seen[0][-1].endswith("head=nbramia%3Afeature&per_page=100")


def test_same_named_fork_branch_does_not_conflict_with_exact_source_identity():
    pr = _same_repo_pr()
    rows = [[
        _head_row(pr.number, pr.head_repository, pr.head_ref),
        _head_row(45, "someone-else/LifeOS", pr.head_ref),
    ]]
    assert publisher._sharing_open_pull_requests(
        "nbramia/LifeOS", pr,
        run=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr=""),
    ) == ()
