"""Focused checks for the checked-in trusted-CI boundary and setup audit."""
from __future__ import annotations

import json
import os
import re
import subprocess
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
    assert workflow.index("name: Bind dispatched runner") < workflow.index("name: Install the declared CPU test environment")
    assert "create-github-app-token" in workflow
    assert workflow.index("name: candidate-execution") < workflow.index("name: candidate-verification-publisher")
    publisher = workflow[workflow.index("name: candidate-verification-publisher"):]
    assert "actions/checkout" not in publisher
    assert "--sha \"$CANDIDATE_SHA\"" in workflow
    assert "candidate-verification-${{ github.event.pull_request.number" in workflow


@pytest.mark.unit
def test_candidate_workflow_pins_actions_and_proves_cpu_wheel_identity():
    workflow = (ROOT / ".github/workflows/candidate-verification.yml").read_text()
    pinned_actions = {
        "actions/checkout": "11bd71901bbe5b1630ceea73d27597364c9af683",
        "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
        "actions/create-github-app-token": "5d869da34e18e7287c1daad50e0b8ea0f506ce69",
        "actions/github-script": "60a0d83039c74a4aee543508d2ffcb1c3799cdea",
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
def test_candidate_workflow_lane_selection_defaults_to_every_lane_before_narrowing():
    """Lane selection must fail closed.

    The gate skips the server-free browser lane only when the candidate's diff
    touches no ``web/`` path. Every other outcome — an unavailable diff, an
    empty diff, a fetch failure — has to run every retained lane, so the
    default assignment precedes any narrowing and the narrowing sits inside a
    guard that requires a successful, non-empty diff.
    """
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/candidate-verification.yml").read_text())
    steps = workflow["jobs"]["execute-candidate"]["steps"]
    selection = next(s for s in steps if "Select the lanes" in (s.get("name") or ""))
    script = selection["run"]

    default_at = script.index("LANES=fast-unit,browser-free")
    narrow_at = script.index("LANES=fast-unit\n")
    assert default_at < narrow_at, "the every-lane default must precede any narrowing"

    # The narrowing is reachable only through a successful, non-empty diff.
    guard = script[:narrow_at]
    assert "git -C candidate diff --name-only" in guard
    assert '[ -n "$CHANGED" ]' in guard

    # Anchored, so a path merely containing "web/" cannot suppress the lane.
    assert "grep -q '^web/'" in script

    verify = next(s for s in steps if "Verify the retained lanes" in (s.get("name") or ""))
    assert '--lanes "$LANES"' in verify["run"], "the verifier must consume the selected lanes"


@pytest.mark.unit
def test_publisher_treats_every_non_success_execution_result_as_a_failed_check():
    """The publisher must recognize exactly one passing value.

    `needs` resolves to `success` only when every job it names succeeded —
    and when the execution job becomes several concurrent parts, only when
    every part succeeded. Everything else (a failure, a cancellation at the
    ceiling, a job that never started) has to publish failure, which
    requires the mapping to allow-list `success` rather than deny-list the
    outcomes anyone happened to think of.
    """
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/candidate-verification.yml").read_text())
    publisher = workflow["jobs"]["publish-aggregate"]
    assert publisher["needs"] == ["execute-candidate"]
    assert publisher["if"] == "${{ always() }}"
    script = next(
        step for step in publisher["steps"] if "github-script" in (step.get("uses") or "")
    )["with"]["script"]
    assert "process.env.RESULT === 'success' ? 'success' : 'failure'" in script


@pytest.mark.unit
def test_lane_command_records_slowest_test_durations():
    """The lane command captures durations so gate cost can be attributed."""
    source = (ROOT / "scripts/verify_candidate.py").read_text()
    assert '"--durations=25"' in source
    assert '"--durations-min=1.0"' in source
