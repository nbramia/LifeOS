"""Production candidate publisher for the atomic two-parent merge design.

Order of operations is load-bearing: this module constructs the final
normalized candidate commit FIRST, then dispatches isolated verification
against that EXACT commit SHA, and only publishes once a required check
bound to a non-candidate-controllable identity confirms it passed. It never
verifies a PR head in isolation and then builds a separate, untested merged
tree — the tree that gets tested is byte-for-byte the tree that gets
published.

A cross-repository (fork) pull request is refused outright before any other
work: the atomic compare-and-swap this module relies on is a single-repo git
receive-pack transaction, and there is no equivalent transaction spanning two
repositories. This is a publication-authorization boundary, not merely a
cleanup nicety — a fork PR must never be silently published through a
same-repo-only mechanism that cannot actually protect it from a source-side
race.

Isolated verification runs on GitHub-hosted infrastructure via
``workflow_dispatch`` against the repository's own trusted workflow
definition (resolved from the base branch, never from the candidate's own
tree) — this module never executes candidate code itself. The required
check that authorizes publication must be issued by a dedicated identity
whose credentials are never available to any workflow the candidate can
influence; see ``docs/guides/candidate-verification-ci.md`` for the exact
trust boundary and setup.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from urllib.parse import urlencode
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

Runner = Callable[..., subprocess.CompletedProcess]


class PublisherError(RuntimeError):
    """A publication step failed in a way that must stop the whole run."""


class ForkPublicationRefusedError(PublisherError):
    """A cross-repository (fork) PR cannot use this same-repo atomic mechanism."""


class MergeConflictError(PublisherError):
    """The real three-way merge could not be constructed automatically."""


class RequiredCheckError(PublisherError):
    """The required check for the exact candidate SHA did not reach success."""


class SharedHeadPublicationRefusedError(PublisherError):
    """Another open PR uses the same source repository and branch."""


# GitHub's generic Actions app and wildcard issuer are not dedicated trust
# anchors.  Keep this production guard here (rather than relying on setup
# auditing) because the publisher is also a directly callable CLI boundary.
GENERIC_ACTIONS_APP_ID = 15368


def _validate_production_issuer(trusted_app_id: int) -> None:
    if isinstance(trusted_app_id, bool) or not isinstance(trusted_app_id, int):
        raise PublisherError("trusted_app_id must be a positive dedicated GitHub App id")
    if trusted_app_id <= 0:
        raise PublisherError("trusted_app_id must be a positive dedicated GitHub App id; -1 means any issuer")
    if trusted_app_id == GENERIC_ACTIONS_APP_ID:
        raise PublisherError(
            f"trusted_app_id {GENERIC_ACTIONS_APP_ID} is GitHub's generic Actions app, not a dedicated issuer"
        )


def _dedicated_app_id_arg(value: str) -> int:
    import argparse

    try:
        app_id = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("trusted app id must be an integer") from exc
    try:
        _validate_production_issuer(app_id)
    except PublisherError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return app_id


def _run(run: Runner, args: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    result = run(list(args), **kwargs)
    return result


def _git(run: Runner, repo: Path, args: Sequence[str], *, check: bool = True) -> str:
    result = _run(run, ["git", "-C", str(repo), *args])
    if check and result.returncode != 0:
        raise PublisherError(
            f"git {' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


# ---------------------------------------------------------------------------
# Step 1: identify the pull request and refuse forks up front.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PullRequestRef:
    number: int
    base_ref: str
    base_repository: str
    head_sha: str
    head_ref: str
    head_repository: Optional[str]
    is_cross_repository: bool
    title: str
    body: str


def fetch_pull_request(repository: str, number: int, *, run: Runner = subprocess.run) -> PullRequestRef:
    """Read the pull request identity and reject fork pull requests.

    ``isCrossRepository`` is GitHub's own signal for "the head lives in a
    different repository" — a fork PR always reports true here, regardless
    of visibility or permissions, so this check does not depend on comparing
    owner names ourselves.
    """
    result = _run(run, [
        "gh", "pr", "view", str(number), "--repo", repository,
        "--json", "number,baseRefName,headRefName,headRefOid,headRepository,headRepositoryOwner,isCrossRepository,title,body",
    ])
    if result.returncode != 0:
        raise PublisherError(f"could not read PR #{number}: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    is_fork = bool(data.get("isCrossRepository"))
    if is_fork:
        raise ForkPublicationRefusedError(
            f"PR #{number} head is in a different repository (a fork). The atomic "
            f"same-repo compare-and-swap this publisher relies on has no cross-repository "
            f"equivalent — refusing to publish rather than pretend a weaker mechanism is "
            f"equally safe. This is a publication-authorization boundary, not a cleanup "
            f"detail: no partial or best-effort publish is attempted."
        )
    head_repo = data.get("headRepository") or {}
    head_repo_name = None
    owner = data.get("headRepositoryOwner") or {}
    if head_repo.get("name") and owner.get("login"):
        head_repo_name = f"{owner['login']}/{head_repo['name']}"
    return PullRequestRef(
        number=number,
        base_ref=data["baseRefName"],
        base_repository=repository,
        head_sha=data["headRefOid"],
        head_ref=data.get("headRefName", ""),
        head_repository=head_repo_name,
        is_cross_repository=is_fork,
        title=data.get("title", ""),
        body=data.get("body", ""),
    )


def _sharing_open_pull_requests(
    repository: str, pr: PullRequestRef, *, run: Runner = subprocess.run,
) -> tuple[int, ...]:
    """Return other open PR numbers using this exact head repository/ref.

    This is a point-in-time GitHub API observation, not a lock: a different
    PR can still open after the last query and before receive-pack. The source
    SHA lease remains the final content-race guard; stronger metadata
    exclusion requires branch policy or a unique source-ref convention.
    """
    if not pr.head_repository or not pr.head_ref:
        raise PublisherError(
            f"PR #{pr.number} has incomplete head repository/ref identity; refusing publication"
        )
    owner = pr.head_repository.split("/", 1)[0]
    query = urlencode({
        "state": "open",
        "head": f"{owner}:{pr.head_ref}",
        "per_page": "100",
    })
    result = _run(run, [
        "gh", "api", "--paginate", "--slurp", f"repos/{repository}/pulls?{query}",
    ])
    if result.returncode != 0:
        raise PublisherError(f"could not inspect open PRs sharing the source branch: {result.stderr.strip()}")
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PublisherError("could not inspect open PRs sharing the source branch: malformed API response") from exc
    if not isinstance(rows, list) or not all(isinstance(page, list) for page in rows):
        raise PublisherError("could not inspect open PRs sharing the source branch: unexpected API response")
    rows = [row for page in rows for row in page]
    sharing: list[int] = []
    target_present = False
    for row in rows:
        if not isinstance(row, dict):
            raise PublisherError("could not inspect open PRs sharing the source branch: malformed PR entry")
        try:
            number = int(row["number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PublisherError("could not inspect open PRs sharing the source branch: missing PR number") from exc
        head = row.get("head") or {}
        row_repo = head.get("repo") if isinstance(head, dict) else None
        if not isinstance(row_repo, dict):
            raise PublisherError("could not inspect open PRs sharing the source branch: malformed head identity")
        row_full_name = row_repo.get("full_name")
        row_ref = head.get("ref") if isinstance(head, dict) else None
        if not row_full_name or not row_ref:
            raise PublisherError("could not inspect open PRs sharing the source branch: incomplete head identity")
        matches_source = row_full_name == pr.head_repository and row_ref == pr.head_ref
        if number == pr.number:
            if not matches_source:
                raise PublisherError(
                    f"could not inspect open PRs sharing the source branch: PR #{pr.number} "
                    "does not match its expected source identity"
                )
            target_present = True
            continue
        if matches_source:
            sharing.append(number)
    if not target_present:
        raise PublisherError(
            f"could not inspect open PRs sharing the source branch: PR #{pr.number} "
            "did not appear in its own paginated head query; refusing to trust it"
        )
    return tuple(sorted(sharing))


def _assert_head_exclusive(repository: str, pr: PullRequestRef, *, run: Runner = subprocess.run) -> None:
    sharing = _sharing_open_pull_requests(repository, pr, run=run)
    if sharing:
        numbers = ", ".join(f"#{number}" for number in sharing)
        raise SharedHeadPublicationRefusedError(
            f"PR #{pr.number} shares source {pr.head_repository}:{pr.head_ref} with open PR(s) {numbers}; "
            "refusing publication because one atomic source-ref lease cannot safely publish both"
        )


# ---------------------------------------------------------------------------
# Step 2: construct the final candidate BEFORE any verification runs.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CandidateCommit:
    sha: str
    tree: str
    base_sha: str
    head_sha: str
    author_name: str
    author_email: str
    normalized_date: str


def _identity(run: Runner, repo: Path, sha: str, field: str) -> str:
    return _git(run, repo, ["log", "-1", f"--format=%{field}", sha]).strip()


def _distinct_coauthors(run: Runner, repo: Path, base_sha: str, head_sha: str, *, exclude_email: str) -> list[str]:
    """Distinct authors across the pull request commit range, excluding the
    candidate's primary author — the head
    commit is only the LATEST commit, and a PR routinely contains commits
    from a reviewer's fixup, a co-author's pairing commit, or a squash of
    someone else's earlier branch. Order is first-seen (oldest commit
    first) for a stable, readable trailer order; case-insensitive on email
    to avoid double-crediting the same person under differing case.
    """
    log = _git(run, repo, ["log", "--reverse", "--format=%an <%ae>\t%ae", f"{base_sha}..{head_sha}"])
    seen_emails = {exclude_email.lower()}
    coauthors: list[str] = []
    for line in log.splitlines():
        if "\t" not in line:
            continue
        display, email = line.rsplit("\t", 1)
        key = email.strip().lower()
        if key in seen_emails:
            continue
        seen_emails.add(key)
        coauthors.append(display.strip())
    return coauthors


def build_candidate(
    repo: Path,
    base_sha: str,
    head_sha: str,
    *,
    pr_number: int,
    pr_title: str,
    closes_text: str = "",
    run: Runner = subprocess.run,
) -> CandidateCommit:
    """Build the real, normalized two-parent candidate commit.

    Primary attribution uses the pull request head commit author
    identity, not a fixed publisher identity — otherwise GitHub cannot
    resolve the published commit's contribution-graph attribution back to
    the real contributor. A git commit has exactly one author, so a PR whose
    base..head range includes commits from OTHER distinct authors — the
    head commit alone does not represent the full PR — gets each of them
    added as an explicit ``Co-authored-by:`` trailer instead of silently
    dropping their attribution. The committer is the primary author's
    identity; only the timestamp is normalized. Both parents are exactly
    ``[base_sha, head_sha]``, in that order, so the real contributor's
    commits stay reachable — this is what lets GitHub's indirect-merge
    detection mark the source PR truthfully `MERGED` once this candidate
    lands on the base branch.
    """
    author_name = _identity(run, repo, head_sha, "an")
    author_email = _identity(run, repo, head_sha, "ae")
    coauthors = _distinct_coauthors(run, repo, base_sha, head_sha, exclude_email=author_email)
    # %aI is strict ISO-8601 in the author's OWN recorded offset (not
    # converted to UTC) — its first 10 characters are exactly the calendar
    # day scripts/post-commit already normalizes per-commit against
    # (`--date=short`, which is the same local-offset day). Slicing this
    # avoids a second `date`-parsing subprocess call and its portability
    # quirks (git's default %ad format is not reliably parseable by GNU
    # `date -d`, unlike %aI).
    author_date_iso = _identity(run, repo, head_sha, "aI")
    day = author_date_iso[:10]
    normalized_date = f"{day}T12:00:00+00:00"

    merge_worktree = Path(tempfile.mkdtemp(prefix=f"lifeos-candidate-merge-{pr_number}-"))
    merge_worktree.rmdir()  # `git worktree add` requires the path not to already exist
    _git(run, repo, ["worktree", "add", "--detach", "--quiet", str(merge_worktree), base_sha])

    try:
        merge_result = _run(run, ["git", "-C", str(merge_worktree), "merge", "--no-commit", "--no-ff", head_sha])
        if merge_result.returncode != 0:
            raise MergeConflictError(
                f"PR #{pr_number} does not merge cleanly onto the current base "
                f"({base_sha}); automatic candidate construction refuses to guess "
                f"a resolution: {merge_result.stderr.strip() or merge_result.stdout.strip()}"
            )
        tree = _git(run, merge_worktree, ["write-tree"]).strip()
    finally:
        _run(run, ["git", "-C", str(repo), "worktree", "remove", "--force", str(merge_worktree)])

    message_lines = [pr_title.strip() or f"Merge PR #{pr_number}"]
    if closes_text:
        message_lines += ["", closes_text.strip()]
    if coauthors:
        message_lines += [""] + [f"Co-authored-by: {name}" for name in coauthors]
    message = "\n".join(message_lines) + "\n"

    commit_tree_result = _run(
        run,
        ["git", "-C", str(repo), "commit-tree", tree, "-p", base_sha, "-p", head_sha, "-m", message],
        env=_env_overrides(author_name, author_email, normalized_date),
    )
    if commit_tree_result.returncode != 0:
        raise PublisherError(f"commit-tree failed: {commit_tree_result.stderr.strip()}")
    candidate_sha = commit_tree_result.stdout.strip()

    return CandidateCommit(
        sha=candidate_sha, tree=tree, base_sha=base_sha, head_sha=head_sha,
        author_name=author_name, author_email=author_email, normalized_date=normalized_date,
    )


def _env_overrides(name: str, email: str, date: str) -> dict:
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": date,
    })
    return env


# ---------------------------------------------------------------------------
# Step 3: isolated verification of the EXACT candidate SHA (never the head).
# ---------------------------------------------------------------------------

STAGING_REF_PREFIX = "refs/heads/lifeos-candidate"


def staging_ref_for(pr_number: int, candidate_sha: str) -> str:
    return f"{STAGING_REF_PREFIX}/pr-{pr_number}-{candidate_sha[:12]}"


def push_candidate_for_verification(
    repo: Path, remote: str, candidate_sha: str, staging_ref: str, *, run: Runner = subprocess.run,
) -> None:
    """Push only the candidate object+ref — never a branch a `push`-triggered
    workflow would treat as trusted; the workflow that verifies this SHA must
    be triggered explicitly (``workflow_dispatch``) so it resolves from the
    base branch's own trusted definition, not from whatever this candidate's
    tree happens to contain in ``.github/workflows/``.
    """
    refspec = f"{candidate_sha}:{staging_ref}"
    result = _run(run, ["git", "-C", str(repo), "push", remote, refspec])
    if result.returncode != 0:
        raise PublisherError(f"could not push candidate to staging ref {staging_ref}: {result.stderr.strip()}")


def trigger_trusted_verification(
    repository: str, workflow_file: str, candidate_sha: str, *, pr_number: int, trusted_runner_sha: str,
    ref: str = "main",
    run: Runner = subprocess.run,
) -> None:
    """Dispatch the repository's OWN trusted workflow definition (resolved
    from ``ref``, i.e. the base branch — never the candidate) against the
    exact candidate SHA. `workflow_dispatch` always resolves the workflow
    YAML from the ref it targets, independent of any input value, which is
    what keeps a candidate that modified `.github/workflows/**` from ever
    getting its own modified definition executed with any privilege.

    ``pr_number`` is passed through as a second input purely so the
    workflow's own concurrency group can be keyed on stable PR identity
    instead of the ephemeral candidate SHA — otherwise a second candidate
    built for the same PR (e.g. after a rebuild) could never cancel the
    still-running verification of the one it supersedes, since each
    candidate has a distinct SHA.

    ``trusted_runner_sha`` is the exact base commit observed while building
    the candidate. The workflow checks out and proves this SHA rather than
    resolving a mutable branch name after dispatch.
    """
    if not re.fullmatch(r"[0-9a-fA-F]{40}", trusted_runner_sha):
        raise PublisherError("trusted runner revision must be an exact 40-character commit SHA")
    result = _run(run, [
        "gh", "workflow", "run", workflow_file, "--repo", repository, "--ref", ref,
        "-f", f"candidate_sha={candidate_sha}", "-f", f"pr_number={pr_number}",
        "-f", f"trusted_runner_sha={trusted_runner_sha}",
    ])
    if result.returncode != 0:
        raise PublisherError(f"could not dispatch trusted verification: {result.stderr.strip()}")


@dataclass(frozen=True)
class CheckOutcome:
    state: str  # "success" | "failure" | "missing" | "timeout"
    detail: str


def _is_required_check(check: dict, check_name: str, trusted_app_id: Optional[int]) -> bool:
    """Name-and-issuer match for the required check.

    The issuer half is the property that closes the forged-context
    vulnerability a generic Actions-app-only match cannot: a
    candidate-controlled workflow can trivially post a check with a matching
    *name*, but it cannot authenticate as an app whose credentials it was
    never given.
    """
    if check.get("name") != check_name:
        return False
    if trusted_app_id is not None and (check.get("app") or {}).get("id") != trusted_app_id:
        return False
    return True


def _check_recency_key(check: dict) -> tuple[str, int]:
    """Order matching checks newest-last. `started_at` is ISO-8601 UTC, so
    it sorts lexicographically; the check id breaks ties and stands in for a
    missing timestamp, since GitHub check-run ids increase monotonically.
    """
    return (str(check.get("started_at") or ""), int(check.get("id") or 0))


def existing_check_run_ids(
    repository: str,
    candidate_sha: str,
    check_name: str,
    *,
    trusted_app_id: Optional[int] = None,
    run: Runner = subprocess.run,
) -> frozenset[int]:
    """The required-check runs already present on a candidate SHA.

    A candidate SHA is deterministic from its content, so a re-publish of an
    unchanged branch targets a SHA that may already carry verdicts from an
    earlier dispatch. Snapshotting their ids before dispatching lets the wait
    below accept only a check this invocation actually caused.

    A failed query raises rather than returning an empty set: "no existing
    checks" and "could not tell" must not collapse into the same value, since
    the empty set is precisely what would let a stale verdict through.
    """
    result = _run(run, ["gh", "api", f"repos/{repository}/commits/{candidate_sha}/check-runs"])
    if result.returncode != 0:
        raise PublisherError(
            f"could not read existing {check_name!r} checks for {candidate_sha}: {result.stderr.strip()}"
        )
    payload = json.loads(result.stdout)
    return frozenset(
        int(check["id"])
        for check in payload.get("check_runs", [])
        if _is_required_check(check, check_name, trusted_app_id) and check.get("id") is not None
    )


def await_required_check(
    repository: str,
    candidate_sha: str,
    check_name: str,
    *,
    trusted_app_id: Optional[int] = None,
    ignore_check_run_ids: Iterable[int] = (),
    timeout_seconds: float = 1800.0,
    poll_interval_seconds: float = 5.0,
    run: Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> CheckOutcome:
    """Poll for the required check on the exact candidate SHA.

    This low-level helper intentionally permits ``trusted_app_id=None`` for
    disposable mechanics probes and unit tests. It is not a production
    authorization path: ``publish_pull_request`` rejects missing/untrusted
    issuer configuration before any GitHub call and always supplies a
    dedicated app id.

    ``ignore_check_run_ids`` carries the verdicts that already existed on this
    SHA before the caller dispatched its own verification (see
    ``existing_check_run_ids``). They are never accepted, in either direction:
    a stale failure would refuse a candidate that now passes, and a stale
    success would authorize publication on the strength of a run that did not
    verify this dispatch. The second is the graver of the two, so the filter
    is applied before the conclusion is ever read.

    Among the checks that remain, the most recently started completed one
    decides the outcome — never whichever the API happened to list first.
    """
    ignored = frozenset(ignore_check_run_ids)
    deadline = now() + timeout_seconds
    while True:
        result = _run(run, ["gh", "api", f"repos/{repository}/commits/{candidate_sha}/check-runs"])
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            completed = [
                check for check in payload.get("check_runs", [])
                if _is_required_check(check, check_name, trusted_app_id)
                and check.get("id") not in ignored
                and check.get("status") == "completed"
            ]
            if completed:
                newest = max(completed, key=_check_recency_key)
                conclusion = newest.get("conclusion")
                if conclusion == "success":
                    return CheckOutcome("success", f"{check_name} succeeded at {newest.get('completed_at')}")
                return CheckOutcome("failure", f"{check_name} concluded {conclusion}")
        if now() >= deadline:
            return CheckOutcome("timeout", f"no matching completed {check_name} within {timeout_seconds}s")
        sleep(poll_interval_seconds)


# ---------------------------------------------------------------------------
# Step 4: the atomic publish itself — dual explicit leases, never a plain
# non-force push for main (see module docstring and PublishOutcome below for
# why a plain fast-forward check is not equivalent to an exact-value lease).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PublishOutcome:
    accepted: bool
    reason: str
    stderr: str


def publish_atomic(
    repo: Path,
    remote: str,
    candidate_sha: str,
    *,
    main_ref: str,
    expected_main_sha: str,
    source_ref: Optional[str] = None,
    expected_source_sha: Optional[str] = None,
    delete_source: bool = False,
    run: Runner = subprocess.run,
) -> PublishOutcome:
    """Publish ``candidate_sha`` to ``main_ref`` in one atomic git transaction.

    Both refs get an EXPLICIT ``--force-with-lease=<ref>:<expected-sha>``,
    not a plain non-force push for ``main``. A plain non-force push only
    proves "the new value is a descendant of whatever main's current tip
    happens to be" — it does NOT prove main is still at the exact SHA this
    candidate was built against. If main's tip were ever reset to an older
    commit that also happens to be an ancestor of this candidate (e.g. an
    administrative reset), a plain fast-forward check would silently accept
    the push despite main differing from the exact observed SHA — an explicit
    lease value rejects that case because it compares against the exact
    expected SHA, not merely "is my new value newer than whatever is there
    now." ``tests/test_candidate_publisher.py`` proves this distinction
    against a real local bare repository, not by assertion alone.

    The leases are compare-and-swap operations on forward (fast-forward)
    updates, never a way to smuggle a rewrite past either ref's protection:
    before attempting the push, this function refuses locally unless the
    expected main and (when advancing it) expected source are real ancestors
    of ``candidate_sha``. A caller error that passed a candidate not built on
    top of an expected ref would otherwise reach the same receive-pack path
    as a genuine race; checking locally gives a clearer failure.
    """
    ancestor_check = _run(run, ["git", "-C", str(repo), "merge-base", "--is-ancestor", expected_main_sha, candidate_sha])
    if ancestor_check.returncode != 0:
        raise PublisherError(
            f"refusing to publish: {expected_main_sha} (expected main) is not an ancestor of "
            f"candidate {candidate_sha} — this lease may only authorize a forward (fast-forward) "
            f"commit, never a history rewrite"
        )
    args = ["git", "-C", str(repo), "push", "--atomic",
            f"--force-with-lease={main_ref}:{expected_main_sha}"]
    refspecs = [f"{candidate_sha}:{main_ref}"]
    if source_ref is not None:
        if delete_source:
            if expected_source_sha is None:
                raise PublisherError("delete_source requires expected_source_sha for its own lease")
            args.append(f"--force-with-lease={source_ref}:{expected_source_sha}")
            refspecs.append(f":{source_ref}")
        elif expected_source_sha is not None:
            source_ancestor_check = _run(
                run,
                ["git", "-C", str(repo), "merge-base", "--is-ancestor", expected_source_sha, candidate_sha],
            )
            if source_ancestor_check.returncode != 0:
                raise PublisherError(
                    f"refusing to publish: {expected_source_sha} (expected source) is not an ancestor of "
                    f"candidate {candidate_sha} — this lease may only authorize a forward source update"
                )
            args.append(f"--force-with-lease={source_ref}:{expected_source_sha}")
            refspecs.append(f"{candidate_sha}:{source_ref}")
    result = _run(run, [*args, remote, *refspecs])
    if result.returncode == 0:
        return PublishOutcome(True, "published", "")
    stderr = result.stderr.strip()
    if "stale info" in stderr or "(fetch first)" in stderr:
        reason = "source or main advanced since it was observed (lease rejected)"
    elif "non-fast-forward" in stderr:
        reason = "main advanced since it was observed (not a fast-forward of the candidate)"
    elif "required status check" in stderr.lower():
        reason = "required status check missing or failing on the candidate SHA"
    else:
        reason = "atomic transaction rejected"
    return PublishOutcome(False, reason, stderr)


def cleanup_source_with_lease(
    repo: Path, remote: str, source_ref: str, expected_source_sha: str, *, run: Runner = subprocess.run,
) -> PublishOutcome:
    """Delete the source ref only if it still matches what was just merged.

    A separate, later lease deletion (never bundled into the same atomic
    transaction as the main advance) — GitHub's own indirect-merge bookkeeping
    needs the merge to be recorded before the source disappears, and a stale
    lease here safely retains a newer source rather than deleting real work.
    """
    result = _run(run, ["git", "-C", str(repo), "push", "--atomic",
                         f"--force-with-lease={source_ref}:{expected_source_sha}",
                         remote, f":{source_ref}"])
    if result.returncode == 0:
        return PublishOutcome(True, "source deleted", "")
    return PublishOutcome(False, "source advanced since merge; retained newer source", result.stderr.strip())


# ---------------------------------------------------------------------------
# End-to-end orchestration: fetch -> build -> stage -> verify -> publish ->
# cleanup, in exactly that order. Every step above is independently testable
# without network access; this function wires them together for the one
# real entry point (the CLI below) that actually talks to GitHub.
# ---------------------------------------------------------------------------

_CLOSING_KEYWORD_RE = re.compile(
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(#\d+)", re.IGNORECASE
)


def _closing_references(pr_body: str) -> str:
    """Each "Closes #N"-style reference in the pull request body, normalized
    onto its own line. GitHub links an issue to a pull request from this text
    and closes the issue when the pull request is detected
    merged, regardless of the eventual merge commit's own message — this is
    a belt-and-suspenders copy so the same references also appear in the
    published commit, which triggers GitHub's separate default-branch
    commit-message closing behavior.
    """
    refs = [f"Closes {m.group(2)}" for m in _CLOSING_KEYWORD_RE.finditer(pr_body or "")]
    return "\n".join(dict.fromkeys(refs))  # de-duplicate, preserve order


@dataclass(frozen=True)
class PublishResult:
    candidate: CandidateCommit
    publish: PublishOutcome
    cleanup: Optional[PublishOutcome]


def publish_pull_request(
    repository: str,
    pr_number: int,
    *,
    repo: Path,
    trusted_app_id: int,
    remote: str = "origin",
    workflow_file: str,
    check_name: str,
    delete_source: bool = True,
    expected_head_sha: Optional[str] = None,
    check_timeout_seconds: float = 1800.0,
    check_poll_interval_seconds: float = 5.0,
    run: Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> PublishResult:
    """Publish one same-repository PR through the full corrected flow.

    Order matches the module docstring exactly: the candidate is built
    (Step 2) before any verification is dispatched (Step 3), and the
    required check must reach ``success`` — bound to ``trusted_app_id`` —
    before ``publish_atomic`` (Step 4) is ever attempted. ``trusted_app_id``
    is required, not optional, here: this is the production entry point,
    and a name-only check match is exactly the forged-context vulnerability
    this design exists to close, so this entry point never falls back to
    it silently. The staging ref used for verification is removed
    afterward regardless of outcome; it is internal bookkeeping, not the
    PR's own source branch.

    The main advance and the source ref are published in ONE atomic
    transaction (main advances to the candidate, and the source ref is
    exact-leased and forward-advanced to the SAME candidate — never bundled
    as a deletion here): if the source ref moved during verification, the whole
    transaction — main included — is rejected rather than merging a
    candidate that fails to reflect the pull request's live state. Source-branch deletion
    follows a successful transaction as a separate,
    later, independently-leased step against the candidate SHA the source
    ref was advanced to by the atomic transaction rather than the pull request's initial
    head SHA — see
    ``cleanup_source_with_lease``. A source branch that gained new commits
    in that later window is safely retained instead of deleted.

    When ``delete_source`` is false, the source branch intentionally remains
    at the candidate SHA after the atomic publish; it is not restored to the
    original PR head. This preserves GitHub's native merged-PR bookkeeping
    while allowing operators to retain the source ref. Before construction
    and again immediately before publication, this entry point checks that no
    other open PR shares the same head repository/ref. These are point-in-time
    API checks, not a GitHub metadata lock; the dual ref leases still protect
    the final SHA race, while a branch policy or unique-ref convention would
    be needed to exclude a PR opened in the final API-to-push gap.

    ``expected_head_sha``, when given, must equal the pull request's current head
    before anything else happens. A caller that already validated some
    other evidence (e.g. a suite-verification record) against a specific
    head SHA passes it here so a commit pushed to the pull request in the gap between
    that validation and this call can never be silently substituted in —
    this function fetches the head fresh, it does not trust a value handed
    to it, so without this check a fresher, unverified head would just be
    published instead.
    """
    _validate_production_issuer(trusted_app_id)
    pr = fetch_pull_request(repository, pr_number, run=run)  # raises for a fork PR
    if expected_head_sha is not None and pr.head_sha != expected_head_sha:
        raise PublisherError(
            f"PR #{pr_number} head is {pr.head_sha}, not the expected {expected_head_sha} — "
            f"refusing to publish a commit that was not the one validated"
        )
    _assert_head_exclusive(repository, pr, run=run)

    _git(run, repo, ["fetch", remote, pr.base_ref])
    expected_main_sha = _git(run, repo, ["rev-parse", f"{remote}/{pr.base_ref}"]).strip()
    _git(run, repo, ["fetch", remote, f"pull/{pr.number}/head"])

    candidate = build_candidate(
        repo, expected_main_sha, pr.head_sha,
        pr_number=pr.number, pr_title=pr.title, closes_text=_closing_references(pr.body), run=run,
    )

    staging_ref = staging_ref_for(pr.number, candidate.sha)
    push_candidate_for_verification(repo, remote, candidate.sha, staging_ref, run=run)
    try:
        # Snapshot before dispatching, so the wait below can tell this
        # invocation's verdict from one already on the SHA. Re-publishing an
        # unchanged branch rebuilds the identical candidate, which is a normal
        # recovery path rather than an error.
        preexisting_checks = existing_check_run_ids(
            repository, candidate.sha, check_name, trusted_app_id=trusted_app_id, run=run,
        )
        trigger_trusted_verification(
            repository, workflow_file, candidate.sha, pr_number=pr.number,
            trusted_runner_sha=expected_main_sha, ref=pr.base_ref, run=run,
        )
        check = await_required_check(
            repository, candidate.sha, check_name,
            trusted_app_id=trusted_app_id, ignore_check_run_ids=preexisting_checks,
            timeout_seconds=check_timeout_seconds,
            poll_interval_seconds=check_poll_interval_seconds, run=run, sleep=sleep, now=now,
        )
        if check.state != "success":
            raise RequiredCheckError(
                f"required check {check_name!r} did not succeed for candidate {candidate.sha}: {check.detail}"
            )

        _assert_head_exclusive(repository, pr, run=run)
        source_ref = f"refs/heads/{pr.head_ref}" if pr.head_ref else None
        publish = publish_atomic(
            repo, remote, candidate.sha,
            main_ref=f"refs/heads/{pr.base_ref}", expected_main_sha=expected_main_sha,
            source_ref=source_ref, expected_source_sha=pr.head_sha if source_ref else None,
            delete_source=False,
            run=run,
        )
    finally:
        _run(run, ["git", "-C", str(repo), "push", remote, "--delete", staging_ref])

    cleanup: Optional[PublishOutcome] = None
    if publish.accepted and delete_source and pr.head_ref:
        # The atomic push above already advanced the source ref to
        # candidate.sha (not pr.head_sha, which it has superseded) — the
        # cleanup lease must match what is actually on the ref now.
        cleanup = cleanup_source_with_lease(repo, remote, f"refs/heads/{pr.head_ref}", candidate.sha, run=run)

    return PublishResult(candidate=candidate, publish=publish, cleanup=cleanup)


def _build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="owner/name, e.g. nbramia/LifeOS")
    parser.add_argument("--pr", type=int, required=True, dest="pr_number")
    parser.add_argument("--repo-path", type=Path, default=Path("."), dest="repo")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--workflow-file", required=True, help="workflow filename dispatched for isolated verification")
    parser.add_argument("--check-name", required=True, help="required check context to await on the candidate SHA")
    parser.add_argument(
        "--trusted-app-id", type=_dedicated_app_id_arg, required=True,
        help="required: the dedicated check-issuer app id; a name-only match is the forged-context vulnerability this design closes",
    )
    parser.add_argument("--no-delete-source", action="store_false", dest="delete_source")
    parser.add_argument("--expected-head-sha", default=None, help="refuse unless the PR head still equals this SHA")
    parser.add_argument("--check-timeout-seconds", type=float, default=1800.0)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    result = publish_pull_request(
        args.repository, args.pr_number,
        repo=args.repo, remote=args.remote,
        workflow_file=args.workflow_file, check_name=args.check_name,
        trusted_app_id=args.trusted_app_id, delete_source=args.delete_source,
        expected_head_sha=args.expected_head_sha,
        check_timeout_seconds=args.check_timeout_seconds,
    )
    print(f"candidate: {result.candidate.sha}")
    print(f"publish: {'accepted' if result.publish.accepted else 'REJECTED'} — {result.publish.reason}")
    if result.cleanup is not None:
        print(f"source cleanup: {'accepted' if result.cleanup.accepted else 'retained'} — {result.cleanup.reason}")
    return 0 if result.publish.accepted else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
