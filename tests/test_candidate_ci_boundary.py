"""Focused checks for the checked-in trusted-CI boundary and setup audit."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.candidate_ci_setup import (
    CandidateCiSetupError,
    audit,
    audit_environment,
    require_app_scoped_gate,
    require_environment_locked_to_main,
)


ROOT = Path(__file__).resolve().parent.parent


def _runner(responses: dict[str, dict]):
    def run(command, **_kwargs):
        endpoint = command[-1]
        return SimpleNamespace(returncode=0, stdout=json.dumps(responses[endpoint]))
    return run


@pytest.mark.unit
def test_candidate_workflow_separates_untrusted_execution_from_status_publisher():
    workflow = (ROOT / ".github/workflows/candidate-verification.yml").read_text()
    execute = workflow[workflow.index("  execute-candidate:"):workflow.index("  publish-aggregate:")]
    assert "pull_request_target:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "candidate_sha:" in workflow
    # merge_group/required-workflow rulesets are Enterprise/org-only and
    # structurally unavailable on this User-owned repository — must not be
    # reintroduced as a dead trigger nobody's plan can ever fire.
    assert "merge_group:" not in workflow
    assert "runs-on: ubuntu-latest" in workflow
    assert "persist-credentials: false" in workflow
    assert "checks: write" in workflow
    assert "python -m playwright install --with-deps chromium" in workflow
    assert "candidate/requirements.txt" in workflow
    assert "path: trusted-runner" in workflow
    assert "path: candidate" in workflow
    assert "python trusted-runner/scripts/verify_candidate.py" in workflow
    assert "candidate-verification-shadow" in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "github.event.pull_request.head.sha || github.event.inputs.candidate_sha" in workflow
    assert "github.event.pull_request.base.sha || github.event.inputs.trusted_runner_sha" in workflow
    assert 'test "$(git -C trusted-runner rev-parse HEAD)" = "$TRUSTED_RUNNER_SHA"' in workflow
    assert "verified by runner ${process.env.TRUSTED_RUNNER_SHA}" in workflow
    assert "WORKFLOW_SHA: ${{ github.workflow_sha }}" in workflow
    assert 'test "$TRUSTED_RUNNER_SHA" = "$WORKFLOW_SHA"' in workflow
    assert 'git -C candidate cat-file commit "$CANDIDATE_SHA"' in workflow
    assert 'test "$FIRST_PARENT" = "$TRUSTED_RUNNER_SHA"' in workflow
    assert execute.index("name: Bind dispatched runner") < execute.index("name: Install the declared CPU test environment")

    # Lane selection is a trusted decision taken before any environment is
    # built: a docs-only candidate installs and executes nothing, and the
    # publisher records that mode explicitly rather than inferring success
    # from an absent job.
    assert execute.index("name: Bind dispatched runner") < execute.index("name: Select the lanes") < execute.index("actions/setup-python")
    assert "python3 trusted-runner/scripts/candidate_lanes.py" in workflow
    assert "verification_mode: ${{ steps.reuse.outputs.mode || steps.select.outputs.mode }}" in workflow
    executed = "steps.select.outputs.mode == 'executed'"
    for step in ("actions/setup-python", "name: Install the declared CPU test environment", "name: Verify the retained lanes"):
        block = execute[execute.index(step):]
        block = block[:block.index("\n      - ")]
        assert executed in block, step
    for step in ("name: Prove checkout identity", "name: Bind dispatched runner"):
        block = execute[execute.index(step):]
        block = block[:block.index("\n      - ")]
        assert executed not in block, step
    assert "create-github-app-token" in workflow
    assert workflow.index("name: candidate-execution") < workflow.index("name: candidate-verification-publisher")
    publisher = workflow[workflow.index("name: candidate-verification-publisher"):]
    assert "VERIFICATION_MODE: ${{ needs.execute-candidate.outputs.verification_mode }}" in publisher
    assert "mode === 'docs-only'" in publisher
    assert "process.env.RESULT === 'success' && explicit" in publisher

    # A dispatched candidate may reuse its head's shadow verdict, but only via
    # the runner's own decision script over App-published check data, read
    # with a read-only token before any environment exists; the shadow run
    # itself never reuses anything.
    reuse = execute[execute.index("name: Reuse a passing shadow verification"):]
    reuse = reuse[:reuse.index("\n      - ")]
    assert "github.event_name == 'workflow_dispatch'" in reuse
    assert "vars.LIFEOS_CANDIDATE_APP_ID != ''" in reuse
    assert "python3 trusted-runner/scripts/candidate_reuse.py" in reuse
    assert 'git -C candidate fetch --quiet --depth=1 origin "$HEAD_SHA"' in reuse
    assert '[ "$(git -C candidate rev-parse "$HEAD_SHA^{tree}")" != "$TREE" ]' in reuse
    assert execute.index("name: Select the lanes") < execute.index("name: Reuse a passing shadow verification") < execute.index("actions/setup-python")
    for step in ("actions/setup-python", "name: Install the declared CPU test environment", "name: Verify the retained lanes"):
        block = execute[execute.index(step):]
        block = block[:block.index("\n      - ")]
        assert "steps.reuse.outputs.mode != 'reused'" in block, step
    assert "mode === 'reused'" in publisher
    assert "trusted_runner: process.env.TRUSTED_RUNNER_SHA" in publisher
    assert "tree: process.env.VERIFICATION_TREE" in publisher
    assert "actions/checkout" not in publisher
    assert "--sha \"$CANDIDATE_SHA\"" in workflow
    assert "candidate-verification-${{ github.event.pull_request.number" in workflow


@pytest.mark.unit
def test_partitioned_execution_derives_its_part_count_from_the_matrix():
    """Every part's tests run, and one failing part cannot hide the others.

    The part count is read from the matrix rather than restated beside it:
    a separately declared count that drifts below the matrix length leaves
    a part's tests unselected while every part that did run reports
    success, which is exactly the coverage loss the lane partition exists
    to avoid. Deriving it makes the two impossible to disagree.
    """
    workflow = (ROOT / ".github/workflows/candidate-verification.yml").read_text()
    assert '--part-index "${{ matrix.part }}"' in workflow
    assert '--part-count "${{ strategy.job-total }}"' in workflow
    # A literal count beside the matrix is the drift this guards against.
    assert "PART_COUNT" not in workflow
    # A failing part must not cancel its siblings, so the aggregate reports
    # every part that would have failed rather than only the first.
    assert "fail-fast: false" in workflow
    # Each part uploads its own lane log; a shared name collides.
    assert "lane-logs-${{ env.CANDIDATE_SHA }}-part${{ matrix.part }}" in workflow

    # Receipts (outcomes and durations, never test output) are retained from
    # every part whatever its result; the full lane log only from a failure.
    receipts = workflow[workflow.index("name: Retain the lane-execution receipts"):]
    receipts = receipts[:receipts.index("\n      - ")]
    assert "if: ${{ always() }}" in receipts
    assert "lane-receipts-${{ env.CANDIDATE_SHA }}-part${{ matrix.part }}" in receipts
    assert "lifeos-lane-logs/*.json" in receipts
    assert "retention-days: 30" in receipts
    lane_logs = workflow[workflow.index("name: Retain the lane log from a failed verification"):]
    lane_logs = lane_logs[:lane_logs.index("name: Retain the lane-execution receipts")]
    assert "if: ${{ failure() }}" in lane_logs
    assert "download-artifact" not in workflow

    # The shadow test-impact report is recorded after the lanes ran, from the
    # runner's own script, into a directory the receipts artifact carries but
    # the duration record never globs; the verifier's arguments are untouched.
    verify_at = workflow.index("name: Verify the retained lanes")
    impact_at = workflow.index("name: Record the shadow test-impact selection")
    assert verify_at < impact_at < workflow.index("name: Retain the lane log from a failed verification")
    impact = workflow[impact_at:workflow.index("\n      - ", impact_at)]
    assert "if: ${{ always() && steps.select.outputs.mode == 'executed' && steps.reuse.outputs.mode != 'reused' }}" in impact
    assert "python3 trusted-runner/scripts/test_impact.py" in impact
    assert '--output "$RUNNER_TEMP/lifeos-impact/impact_selection.json"' in impact
    assert "test_impact" not in workflow[verify_at:impact_at]
    # The receipts artifact lists exactly one path, so its root stays the lane
    # log directory and the documented regeneration command finds the
    # receipts at the top level; the report travels in its own artifact.
    assert "path: ${{ runner.temp }}/lifeos-lane-logs/*.json" in receipts
    assert "lifeos-impact" not in receipts
    report = workflow[workflow.index("name: Retain the shadow test-impact selection"):]
    report = report[:report.index("publish-aggregate:")]
    assert "impact-selection-${{ env.CANDIDATE_SHA }}-part${{ matrix.part }}" in report
    assert "path: ${{ runner.temp }}/lifeos-impact/impact_selection.json" in report
    assert "if: ${{ always() }}" in report


@pytest.mark.unit
def test_candidate_workflow_pins_actions_and_proves_cpu_wheel_identity():
    workflow = (ROOT / ".github/workflows/candidate-verification.yml").read_text()
    pinned_actions = {
        "actions/checkout": "11bd71901bbe5b1630ceea73d27597364c9af683",
        "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
        "actions/create-github-app-token": "5d869da34e18e7287c1daad50e0b8ea0f506ce69",
        "actions/github-script": "60a0d83039c74a4aee543508d2ffcb1c3799cdea",
        "actions/cache/restore": "0057852bfaa89a56745cba8c7296529d2fc39830",
        "actions/cache/save": "0057852bfaa89a56745cba8c7296529d2fc39830",
    }
    for action, sha in pinned_actions.items():
        assert f"{action}@{sha}" in workflow
    assert not re.search(r"^\s*uses:\s*[^\s@]+@v\d+\b", workflow, flags=re.MULTILINE)
    assert 'TORCH_CPU_VERSION: "2.14.0+cpu"' in workflow
    assert '--index-url https://download.pytorch.org/whl/cpu -c "$RUNNER_TEMP/cpu-torch-constraints.txt" "torch==$TORCH_CPU_VERSION"' in workflow
    assert 'printf \'torch==%s\\n\' "$TORCH_CPU_VERSION" > "$RUNNER_TEMP/cpu-torch-constraints.txt"' in workflow
    assert '-c "$RUNNER_TEMP/cpu-torch-constraints.txt" -r candidate/requirements.txt' in workflow
    assert "requirements-without-torch" not in workflow
    assert 'assert torch.__version__ == os.environ["TORCH_CPU_VERSION"]' in workflow
    assert "torch.version.cuda is None and torch.version.hip is None" in workflow


@pytest.mark.unit
def test_candidate_workflow_caches_the_test_environment_from_a_trusted_job_only():
    """The cached environment is written by a job that never touches the
    candidate.

    The execution job runs candidate code in its verify step, so it holds
    no cache-write scope and only restores. A separate job checks out the
    protected runner alone, builds wheels-only from its requirements file,
    and saves under the same key shape the execution job restores with; a
    hit in the execution job still proves the torch identity and the
    package fingerprint the build recorded.
    """
    import yaml

    workflow = (ROOT / ".github/workflows/candidate-verification.yml").read_text()
    jobs = yaml.safe_load(workflow)["jobs"]
    prepare = jobs["prepare-environment"]
    execute = jobs["execute-candidate"]
    workflow_yaml = yaml.safe_load(workflow)
    assert workflow_yaml["cache-mode"] == "read"
    assert prepare["cache-mode"] == "write"
    assert "cache-mode" not in execute
    assert "cache-mode" not in jobs["publish-aggregate"]
    assert prepare["permissions"] == {"contents": "read"}
    assert "actions" not in execute["permissions"]
    assert "environment" not in prepare
    prepare_text = workflow[workflow.index("  prepare-environment:"):workflow.index("  execute-candidate:")]
    assert "path: candidate" not in prepare_text
    assert "candidate/" not in prepare_text
    assert "hashFiles('trusted-runner/requirements.txt')" in prepare_text
    assert "-r trusted-runner/requirements.txt" in prepare_text
    assert prepare_text.count("--only-binary=:all:") == 2
    assert "lookup-only: true" in prepare_text
    assert prepare_text.index("name: Build the CPU test environment") < prepare_text.index("name: Save the installed CPU test environment")
    assert prepare["steps"][0]["name"] == "Bind the environment builder to protected workflow provenance"
    assert "key: ${{ steps.env-cache.outputs.cache-primary-key }}" in prepare_text
    assert "actions/cache/save" not in workflow[workflow.index("  execute-candidate:"):]
    execute_text = workflow[workflow.index("  execute-candidate:"):workflow.index("  publish-aggregate:")]
    restore_at = execute_text.index("name: Restore the installed CPU test environment")
    install_at = execute_text.index("name: Install the declared CPU test environment")
    verify_at = execute_text.index("name: Verify the retained lanes")
    assert restore_at < install_at < verify_at
    key = "lifeos-test-env-v1-${{ runner.os }}-py${{ steps.python.outputs.python-version }}-torch${{ env.TORCH_CPU_VERSION }}-"
    assert key + "${{ hashFiles('candidate/requirements.txt') }}-${{ steps.cache-window.outputs.week }}" in execute_text[restore_at:install_at]
    assert key + "${{ hashFiles('trusted-runner/requirements.txt') }}-${{ steps.cache-window.outputs.week }}" in prepare_text
    window = execute_text[execute_text.index("name: Bound the cached environment's age"):restore_at]
    assert 'echo "week=$(date -u +%G-W%V)" >> "$GITHUB_OUTPUT"' in window
    install = execute_text[install_at:verify_at]
    assert 'if [ "$CACHE_HIT" != "true" ]; then' in install
    assert "python -m playwright install-deps chromium" in install
    assert 'test "$(python -m pip freeze --all | LC_ALL=C sort | sha256sum)" = "$(cat "$FINGERPRINT")"' in install
    assert 'echo "$VENV/bin" >> "$GITHUB_PATH"' in install
    assert "--no-binary" not in install
    assert install.count("--only-binary=:all:") == 2
    # The identity proof runs on both paths: it sits after the branch closes.
    assert install.index("          fi\n") < install.index('assert torch.__version__ == os.environ["TORCH_CPU_VERSION"]')


@pytest.mark.unit
def test_candidate_workflow_scopes_the_app_secret_to_an_environment_the_untrusted_job_never_declares():
    """The App private key must be an ENVIRONMENT secret gated to a job that
    declares `environment:`, never a plain repository secret — a repository
    secret is reachable from any same-repo `pull_request`-triggered
    workflow, including one a candidate PR adds itself, which would let it
    mint a valid App token and forge a passing check without running any
    real verification."""
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/candidate-verification.yml").read_text())
    jobs = workflow["jobs"]

    assert "environment" not in jobs["execute-candidate"], (
        "the untrusted candidate-execution job must never declare the publisher's environment"
    )
    assert jobs["publish-aggregate"]["environment"] == "candidate-verification-publish"

    # CHECK_NAME must be defined in publish-aggregate's OWN env — job-level
    # `env:` blocks do not cross job boundaries, so defining it only on
    # execute-candidate (as a prior draft of this file did) leaves
    # `process.env.CHECK_NAME` undefined in the job that actually reads it.
    assert jobs["execute-candidate"].get("env", {}).get("CHECK_NAME") is None
    assert "CHECK_NAME" in jobs["publish-aggregate"]["env"]


@pytest.mark.unit
def test_candidate_workflow_dispatch_carries_pr_number_for_concurrency_grouping():
    """A second candidate built for the same PR (e.g. after a rebuild) has a
    distinct SHA, so a concurrency group keyed on candidate_sha could never
    cancel the still-running verification of the candidate it supersedes —
    the group must be keyed on stable PR identity instead."""
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/candidate-verification.yml").read_text())
    assert "pr_number" in workflow[True]["workflow_dispatch"]["inputs"]
    assert "trusted_runner_sha" in workflow[True]["workflow_dispatch"]["inputs"]
    assert "github.event.inputs.pr_number" in workflow["concurrency"]["group"]


@pytest.mark.unit
def test_provenance_parent_check_works_in_a_real_depth_one_candidate_checkout(tmp_path):
    """The workflow must not use revision traversal that shallow clones lose."""
    bare = tmp_path / "remote.git"
    source = tmp_path / "source"
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    (source / "base.txt").write_text("base")
    subprocess.run(["git", "-C", str(source), "add", "base.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, text=True, capture_output=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(source), "checkout", "-q", "-b", "feature"], check=True)
    (source / "feature.txt").write_text("feature")
    subprocess.run(["git", "-C", str(source), "add", "feature.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "feature"], check=True)
    head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, text=True, capture_output=True,
    ).stdout.strip()
    candidate = subprocess.run(
        ["git", "-C", str(source), "commit-tree", f"{base}^{{tree}}", "-p", base, "-p", head, "-m", "candidate"],
        check=True, text=True, capture_output=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(source), "update-ref", "refs/heads/candidate", candidate], check=True)
    subprocess.run(["git", "-C", str(source), "push", "-q", "origin", "candidate"], check=True)
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "candidate", f"file://{bare}", str(shallow)], check=True)

    script = (
        'first_parent=$(git -C "$1" cat-file commit "$2" | '
        "awk '$1 == \"parent\" { print $2; exit }'); test \"$first_parent\" = \"$3\""
    )
    assert subprocess.run(["bash", "-c", script, "--", str(shallow), candidate, base]).returncode == 0
    assert subprocess.run(["bash", "-c", script, "--", str(shallow), candidate, "f" * 40]).returncode != 0


@pytest.mark.unit
def test_provenance_binding_step_rejects_mismatched_or_forged_runner_sha(tmp_path):
    """Execute the ACTUAL "Bind dispatched runner to protected workflow
    provenance" step script — extracted from the real workflow YAML via
    ``yaml.safe_load``, not hand-copied — against a real depth-one candidate
    clone under bash's ``-eo pipefail`` (GitHub Actions' own default shell).

    A substring assertion on the YAML text (as
    ``test_candidate_workflow_separates_untrusted_execution_from_status_publisher``
    does above) proves the script mentions the right variable names; it does
    not prove the script actually rejects a caller-supplied
    ``trusted_runner_sha`` that mismatches the immutable
    ``github.workflow_sha``, or a candidate whose real first parent doesn't
    match either one. This test proves both, plus the legitimate success
    case, by running the literal production script.
    """
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/candidate-verification.yml").read_text())
    steps = workflow["jobs"]["execute-candidate"]["steps"]
    bind_step = next(s for s in steps if s.get("name") == "Bind dispatched runner to protected workflow provenance")
    script = bind_step["run"]

    bare = tmp_path / "remote.git"
    source = tmp_path / "source"
    candidate_dir = tmp_path / "candidate"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    (source / "base.txt").write_text("base")
    subprocess.run(["git", "-C", str(source), "add", "base.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, text=True, capture_output=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(source), "checkout", "-q", "-b", "feature"], check=True)
    (source / "feature.txt").write_text("feature")
    subprocess.run(["git", "-C", str(source), "add", "feature.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "feature"], check=True)
    head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, text=True, capture_output=True,
    ).stdout.strip()
    candidate = subprocess.run(
        ["git", "-C", str(source), "commit-tree", f"{base}^{{tree}}", "-p", base, "-p", head, "-m", "candidate"],
        check=True, text=True, capture_output=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(source), "update-ref", "refs/heads/candidate", candidate], check=True)
    subprocess.run(["git", "-C", str(source), "push", "-q", "origin", "candidate"], check=True)
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--branch", "candidate", f"file://{bare}", str(candidate_dir)],
        check=True,
    )

    def run_bind_step(*, trusted_runner_sha: str, workflow_sha: str, event_name: str = "workflow_dispatch") -> int:
        env = {
            **os.environ,
            "CANDIDATE_SHA": candidate,
            "TRUSTED_RUNNER_SHA": trusted_runner_sha,
            "EVENT_NAME": event_name,
            "WORKFLOW_SHA": workflow_sha,
        }
        return subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=str(tmp_path), env=env).returncode

    # Legitimate dispatch: the dispatched trusted_runner_sha equals the
    # immutable workflow_sha, and the candidate's real first parent matches.
    assert run_bind_step(trusted_runner_sha=base, workflow_sha=base) == 0
    # Attacker-supplied trusted_runner_sha mismatches the immutable
    # github.workflow_sha (e.g. a caller dispatching with a stale or
    # unrelated value) — must fail closed, before any dependency install.
    assert run_bind_step(trusted_runner_sha="d" * 40, workflow_sha=base) != 0
    # Forged candidate: trusted_runner_sha matches workflow_sha, but the
    # candidate's actual commit-graph first parent is not that commit
    # (i.e. the candidate was not really built on the claimed base).
    assert run_bind_step(trusted_runner_sha=head, workflow_sha=head) != 0
    # The diagnostic pull_request_target path is unchecked by design —
    # those fields are GitHub-set, not attacker input.
    assert run_bind_step(trusted_runner_sha="not-a-sha", workflow_sha="irrelevant", event_name="pull_request_target") == 0


@pytest.mark.unit
def test_setup_audit_never_mistakes_generic_actions_app_for_the_trusted_issuer():
    result = audit("synthetic/repository", run=_runner({
        "repos/synthetic/repository/branches/main/protection": {
            "enforce_admins": {"enabled": True},
            "required_status_checks": {"checks": [{"context": "candidate-verification", "app_id": 15368}]},
        },
    }))
    assert result.classic_contexts == ("candidate-verification",)
    assert result.check_app_ids == {"candidate-verification": 15368}
    with pytest.raises(CandidateCiSetupError, match="any workflow in this repository"):
        require_app_scoped_gate(result, context="candidate-verification", trusted_app_id=987654)


@pytest.mark.unit
def test_setup_audit_never_mistakes_wildcard_app_for_the_trusted_issuer():
    result = audit("synthetic/repository", run=_runner({
        "repos/synthetic/repository/branches/main/protection": {
            "enforce_admins": {"enabled": True},
            "required_status_checks": {"checks": [{"context": "candidate-verification", "app_id": -1}]},
        },
    }))
    with pytest.raises(CandidateCiSetupError, match="any workflow in this repository"):
        require_app_scoped_gate(result, context="candidate-verification", trusted_app_id=987654)


@pytest.mark.unit
def test_setup_requires_admin_enforcement_even_with_the_trusted_app_bound():
    result = audit("synthetic/repository", run=_runner({
        "repos/synthetic/repository/branches/main/protection": {
            "enforce_admins": {"enabled": False},
            "required_status_checks": {"checks": [{"context": "candidate-verification", "app_id": 987654}]},
        },
    }))
    with pytest.raises(CandidateCiSetupError, match="enforce administrators"):
        require_app_scoped_gate(result, context="candidate-verification", trusted_app_id=987654)


@pytest.mark.unit
def test_setup_audit_accepts_the_trusted_app_bound_check():
    result = audit("synthetic/repository", run=_runner({
        "repos/synthetic/repository/branches/main/protection": {
            "enforce_admins": {"enabled": True},
            "required_status_checks": {"checks": [{"context": "candidate-verification", "app_id": 987654}]},
        },
    }))
    require_app_scoped_gate(result, context="candidate-verification", trusted_app_id=987654)  # does not raise


@pytest.mark.unit
def test_environment_audit_rejects_the_protected_branches_fallback():
    """The 'protected branches' deployment policy fallback admits every
    branch GitHub currently marks protected — a no-op boundary unless the
    operator has verified something is actually protected, which this
    command does not assume."""
    result = audit_environment("synthetic/repository", "candidate-verification-publish", run=_runner({
        "repos/synthetic/repository/environments/candidate-verification-publish": {
            "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False},
        },
    }))
    with pytest.raises(CandidateCiSetupError, match="protected branches"):
        require_environment_locked_to_main(result)


@pytest.mark.unit
def test_environment_audit_rejects_no_policy_configured():
    result = audit_environment("synthetic/repository", "candidate-verification-publish", run=_runner({
        "repos/synthetic/repository/environments/candidate-verification-publish": {
            "deployment_branch_policy": None,
        },
    }))
    with pytest.raises(CandidateCiSetupError, match="no deployment branch policy"):
        require_environment_locked_to_main(result)


@pytest.mark.unit
def test_environment_audit_rejects_a_policy_naming_more_than_just_main():
    result = audit_environment("synthetic/repository", "candidate-verification-publish", run=_runner({
        "repos/synthetic/repository/environments/candidate-verification-publish": {
            "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
        },
        "repos/synthetic/repository/environments/candidate-verification-publish/deployment-branch-policies": {
            "branch_policies": [{"name": "main"}, {"name": "feature/*"}],
        },
    }))
    with pytest.raises(CandidateCiSetupError, match=r"not exactly \('main',\)"):
        require_environment_locked_to_main(result)


@pytest.mark.unit
def test_environment_audit_accepts_a_policy_naming_exactly_main():
    result = audit_environment("synthetic/repository", "candidate-verification-publish", run=_runner({
        "repos/synthetic/repository/environments/candidate-verification-publish": {
            "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
        },
        "repos/synthetic/repository/environments/candidate-verification-publish/deployment-branch-policies": {
            "branch_policies": [{"name": "main"}],
        },
    }))
    require_environment_locked_to_main(result)  # does not raise


@pytest.mark.unit
def test_candidate_workflow_lane_selection_is_the_trusted_runner_script():
    """Lane selection is one trusted decision, taken before anything is built.

    The step feeds the changed set to the runner's own ``candidate_lanes.py``
    (whose fail-closed rules ``tests/test_candidate_lanes.py`` pins: an empty
    or unavailable diff runs every retained lane, only a ``web/`` path keeps
    the browser lane, and only the docs-only rule executes nothing) and the
    verifier consumes exactly the lanes that script chose. The changed set
    prefers the merge-base diff and falls back to the two-commit diff, so a
    computation failure degrades to a superset rather than to nothing.
    """
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/candidate-verification.yml").read_text())
    steps = workflow["jobs"]["execute-candidate"]["steps"]
    selection = next(s for s in steps if "Select the lanes" in (s.get("name") or ""))
    assert selection["id"] == "select"
    script = selection["run"]
    assert 'git -C candidate fetch --no-tags --depth=1 origin "$TRUSTED_RUNNER_SHA"' in script
    assert script.index("fetch --no-tags") < script.index("diff --name-only --merge-base")
    assert '|| git -C candidate diff --name-only "$TRUSTED_RUNNER_SHA" "$CANDIDATE_SHA"' in script
    assert "python3 trusted-runner/scripts/candidate_lanes.py" in script
    assert "candidate/scripts" not in script
    assert 'sed -n \'s/^lanes=/LANES=/p\'' in script
    outputs = workflow["jobs"]["execute-candidate"]["outputs"]
    assert outputs["verification_mode"] == "${{ steps.reuse.outputs.mode || steps.select.outputs.mode }}"
    assert outputs["verification_tree"] == "${{ steps.select.outputs.tree }}"
    assert outputs["verification_lanes"] == "${{ steps.select.outputs.lanes }}"
    assert outputs["reused_check_id"] == "${{ steps.reuse.outputs.reused_check_id }}"
    assert outputs["reused_candidate"] == "${{ steps.reuse.outputs.reused_candidate }}"
    publisher = workflow["jobs"]["publish-aggregate"]
    script_step = next(s for s in publisher["steps"] if "github-script" in (s.get("uses") or ""))
    assert script_step["env"]["REUSED_CHECK_ID"] == "${{ needs.execute-candidate.outputs.reused_check_id }}"
    assert script_step["env"]["REUSED_CANDIDATE"] == "${{ needs.execute-candidate.outputs.reused_candidate }}"
    assert "process.env.REUSED_CHECK_ID" in script_step["with"]["script"]
    assert "process.env.REUSED_CANDIDATE" in script_step["with"]["script"]
    assert workflow["jobs"]["execute-candidate"]["permissions"] == {"contents": "read", "checks": "read"}

    verify = next(s for s in steps if "Verify the retained lanes" in (s.get("name") or ""))
    assert '--lanes "$LANES"' in verify["run"], "the verifier must consume the selected lanes"
    assert verify["if"] == "${{ steps.select.outputs.mode == 'executed' && steps.reuse.outputs.mode != 'reused' }}"


@pytest.mark.unit
def test_publisher_treats_every_non_success_execution_result_as_a_failed_check():
    """The publisher must recognize exactly one passing value.

    `needs` resolves to `success` only when every job it names succeeded —
    and when the execution job becomes several concurrent parts, only when
    every part succeeded. Everything else (a failure, a cancellation at the
    ceiling, a job that never started) has to publish failure, which
    requires the mapping to allow-list `success` rather than deny-list the
    outcomes anyone happened to think of. Success additionally requires an
    explicit verification mode, so a job that passed without ever reaching
    its selection step cannot publish green.
    """
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/candidate-verification.yml").read_text())
    publisher = workflow["jobs"]["publish-aggregate"]
    assert publisher["needs"] == ["execute-candidate"]
    assert publisher["if"] == "${{ always() }}"
    script = next(
        step for step in publisher["steps"] if "github-script" in (step.get("uses") or "")
    )["with"]["script"]
    assert "process.env.RESULT === 'success' && explicit ? 'success' : 'failure'" in script
    assert "const explicit = mode === 'executed' || mode === 'docs-only' || mode === 'reused'" in script


@pytest.mark.unit
def test_lane_command_records_slowest_test_durations():
    """The lane command captures durations so gate cost can be attributed."""
    source = (ROOT / "scripts/verify_candidate.py").read_text()
    assert '"--durations=25"' in source
    assert '"--durations-min=1.0"' in source



def _lane_selection_step_run() -> str:
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/candidate-verification.yml").read_text())
    steps = workflow["jobs"]["execute-candidate"]["steps"]
    return next(s for s in steps if "Select the lanes" in (s.get("name") or ""))["run"]


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _two_shallow_checkouts(tmp_path, head_files: dict):
    """An origin with a base commit and a head commit on top, cloned the way
    the workflow does: `trusted-runner` at the base, `candidate` at the head,
    each a separate depth-1 clone sharing no objects."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "base")
    _git(origin, "config", "user.email", "ci@example.invalid")
    _git(origin, "config", "user.name", "ci")
    _git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    (origin / "README.md").write_text("base\n")
    (origin / "api").mkdir()
    (origin / "api" / "x.py").write_text("X = 1\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "base")
    base_sha = _git(origin, "rev-parse", "HEAD")
    _git(origin, "checkout", "-q", "-b", "main")
    for rel, body in head_files.items():
        path = origin / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "head")
    head_sha = _git(origin, "rev-parse", "HEAD")
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "clone", "-q", "--depth=1", "--branch", "base", f"file://{origin}", str(work / "trusted-runner")], check=True)
    subprocess.run(["git", "clone", "-q", "--depth=1", "--branch", "main", f"file://{origin}", str(work / "candidate")], check=True)
    (work / "trusted-runner" / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "candidate_lanes.py", work / "trusted-runner" / "scripts" / "candidate_lanes.py")
    return work, base_sha, head_sha


def _run_lane_selection(tmp_path, head_files: dict) -> dict:
    work, base_sha, head_sha = _two_shallow_checkouts(tmp_path, head_files)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    output = tmp_path / "github-output"
    output.touch()
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "TRUSTED_RUNNER_SHA": base_sha, "CANDIDATE_SHA": head_sha,
        "RUNNER_TEMP": str(runner_temp), "GITHUB_OUTPUT": str(output), "GITHUB_ENV": str(tmp_path / "github-env"),
    }
    result = subprocess.run(["bash", "-eo", "pipefail", "-c", _lane_selection_step_run()], cwd=work, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)


@pytest.mark.unit
def test_lane_selection_step_sees_the_real_diff_across_two_shallow_checkouts(tmp_path):
    """The step diffs two commits that live in different depth-1 clones, so it
    must fetch the base into the candidate clone first; without that fetch
    every run degrades to "changed set unavailable" and the docs-only and
    web/ rules never fire."""
    outputs = _run_lane_selection(tmp_path, {"docs/guide.md": "docs\n"})
    assert outputs["mode"] == "docs-only"
    assert outputs["lanes"] == ""
    assert "unavailable" not in outputs["reason"]


@pytest.mark.unit
@pytest.mark.parametrize("head_files,lanes", [
    ({"web/app.js": "1\n"}, "fast-unit,browser-free"),
    ({"api/x.py": "X = 2\n"}, "fast-unit"),
], ids=["web_change", "api_change"])
def test_lane_selection_step_keeps_the_browser_lane_only_for_a_web_change(tmp_path, head_files, lanes):
    outputs = _run_lane_selection(tmp_path, head_files)
    assert (outputs["mode"], outputs["lanes"]) == ("executed", lanes)


def _reuse_step_run() -> str:
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/candidate-verification.yml").read_text())
    steps = workflow["jobs"]["execute-candidate"]["steps"]
    return next(s for s in steps if "Reuse a passing shadow verification" in (s.get("name") or ""))["run"]


@pytest.mark.unit
def test_reuse_step_reads_the_second_parent_from_commit_headers_not_the_message(tmp_path):
    """The candidate is a two-parent commit whose message carries
    candidate-authored text: its title line and closing references. The head the
    reuse step fetches must come from the commit's header lines, so a message
    line that happens to start with `parent <hex>` can never redirect it."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "ci@example.invalid")
    _git(origin, "config", "user.name", "ci")
    (origin / "a.txt").write_text("base\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "base")
    base_sha = _git(origin, "rev-parse", "HEAD")
    (origin / "a.txt").write_text("head\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "head")
    head_sha = _git(origin, "rev-parse", "HEAD")
    forged = "f" * 40
    message = f"candidate\n\nparent {forged}\nparent {forged}\nCloses #1\n"
    candidate_sha = subprocess.run(
        ["git", "-C", str(origin), "commit-tree", f"{head_sha}^{{tree}}", "-p", base_sha, "-p", head_sha, "-m", message],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    work = tmp_path / "work"
    (work / "candidate").mkdir(parents=True)
    shutil.copytree(origin / ".git", work / "candidate" / ".git")
    extraction = _reuse_step_run().split("\n")[0]
    assert extraction.startswith('HEAD_SHA="$(git -C candidate cat-file commit "$CANDIDATE_SHA"')
    result = subprocess.run(
        ["bash", "-e", "-c", extraction + '\nprintf %s "$HEAD_SHA"'], cwd=work,
        env={**os.environ, "CANDIDATE_SHA": candidate_sha}, capture_output=True, text=True, check=True,
    )
    assert result.stdout == head_sha
    assert forged not in result.stdout



@pytest.mark.unit
def test_environment_builder_binding_step_rejects_a_forged_runner_sha():
    """The only cache-writing job runs the same provenance binding the
    execution job does, as its first step: a dispatched run whose
    trusted_runner_sha is not the commit the workflow was resolved from
    fails before anything is checked out, built, or saved. The
    pull_request_target path passes any value because its base SHA is
    GitHub-set, not caller input."""
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/candidate-verification.yml").read_text())
    steps = workflow["jobs"]["prepare-environment"]["steps"]
    assert steps[0]["name"] == "Bind the environment builder to protected workflow provenance"
    script = steps[0]["run"]

    def run(*, trusted_runner_sha: str, workflow_sha: str, event_name: str = "workflow_dispatch") -> int:
        env = {**os.environ, "TRUSTED_RUNNER_SHA": trusted_runner_sha, "EVENT_NAME": event_name, "WORKFLOW_SHA": workflow_sha}
        return subprocess.run(["bash", "-eo", "pipefail", "-c", script], env=env, capture_output=True).returncode

    assert run(trusted_runner_sha="a" * 40, workflow_sha="a" * 40) == 0
    assert run(trusted_runner_sha="b" * 40, workflow_sha="a" * 40) != 0
    assert run(trusted_runner_sha="", workflow_sha="a" * 40) != 0
    assert run(trusted_runner_sha="not-a-sha", workflow_sha="irrelevant", event_name="pull_request_target") == 0
