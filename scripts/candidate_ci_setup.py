"""Audit the GitHub policy prerequisites for trusted candidate verification.

`nbramia/LifeOS` is a User-owned repository: GitHub merge queue and
"required workflows" repository rules are both Enterprise/organization-only
features and are structurally unavailable here, not merely unconfigured —
this audit does not wait on either. The actual gate is a required status
check bound to a specific, dedicated GitHub App id (the candidate publisher's
trusted check issuer, whose credentials are never given to any workflow a
candidate can influence). A required-check entry whose `app_id` is the
generic GitHub Actions app (15368) or the wildcard (-1, "any app may satisfy
this context") is exactly as forgeable as no binding at all — this command
fails closed unless the configured entry's `app_id` matches the operator's
own dedicated app.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional, Sequence


class CandidateCiSetupError(RuntimeError):
    """The repository cannot demonstrate an app-scoped required gate."""


@dataclass(frozen=True)
class PolicyAudit:
    repository: str
    classic_contexts: tuple[str, ...]
    enforce_admins: bool
    check_app_ids: dict[str, Optional[int]]


def _gh(arguments: Sequence[str], run: Callable[..., object] = subprocess.run) -> object:
    result = run(["gh", "api", *arguments], check=False, text=True, capture_output=True)
    if getattr(result, "returncode", 1) != 0:
        raise CandidateCiSetupError("GitHub policy query failed")
    try:
        return json.loads(getattr(result, "stdout", ""))
    except json.JSONDecodeError as exc:
        raise CandidateCiSetupError("GitHub policy response is not JSON") from exc


def audit(repository: str, *, run: Callable[..., object] = subprocess.run) -> PolicyAudit:
    protection = _gh([f"repos/{repository}/branches/main/protection"], run)
    checks = protection.get("required_status_checks", {}).get("checks", [])
    contexts = tuple(sorted(check.get("context", "") for check in checks if isinstance(check, dict)))
    enforce_admins = bool(protection.get("enforce_admins", {}).get("enabled"))
    check_app_ids = {
        check.get("context", ""): check.get("app_id")
        for check in checks if isinstance(check, dict)
    }
    return PolicyAudit(repository, contexts, enforce_admins, check_app_ids)


# GitHub's own generic Actions app. A required-check entry bound only to this
# id (or to app_id -1, "any app") can be satisfied by any workflow in the
# repository with `checks: write` — including one a candidate PR added or
# modified itself — so neither counts as an app-scoped binding.
GITHUB_ACTIONS_APP_ID = 15368


def require_app_scoped_gate(audit_result: PolicyAudit, *, context: str, trusted_app_id: int) -> None:
    if not audit_result.enforce_admins:
        raise CandidateCiSetupError("main protection must enforce administrators")
    configured_app_id = audit_result.check_app_ids.get(context)
    if configured_app_id is None:
        raise CandidateCiSetupError(f"no required check named {context!r} is configured")
    if configured_app_id in (GITHUB_ACTIONS_APP_ID, -1):
        raise CandidateCiSetupError(
            f"required check {context!r} is bound to app_id {configured_app_id}, which any "
            f"workflow in this repository (including one a candidate PR modifies) can satisfy — "
            f"refusing until it is bound to the dedicated trusted app id {trusted_app_id}"
        )
    if configured_app_id != trusted_app_id:
        raise CandidateCiSetupError(
            f"required check {context!r} is bound to app_id {configured_app_id}, not the "
            f"expected trusted app id {trusted_app_id}"
        )


@dataclass(frozen=True)
class EnvironmentAudit:
    environment: str
    protected_branches_fallback: bool
    custom_branch_policies: bool
    branch_policy_names: tuple[str, ...]


def audit_environment(
    repository: str, environment: str, *, run: Callable[..., object] = subprocess.run,
) -> EnvironmentAudit:
    env_info = _gh([f"repos/{repository}/environments/{environment}"], run)
    policy = env_info.get("deployment_branch_policy") or {}
    protected_fallback = bool(policy.get("protected_branches"))
    custom = bool(policy.get("custom_branch_policies"))
    branch_names: tuple[str, ...] = ()
    if custom:
        policies = _gh([f"repos/{repository}/environments/{environment}/deployment-branch-policies"], run)
        branch_names = tuple(sorted(
            p.get("name", "") for p in policies.get("branch_policies", []) if isinstance(p, dict)
        ))
    return EnvironmentAudit(environment, protected_fallback, custom, branch_names)


def require_environment_locked_to_main(audit_result: EnvironmentAudit) -> None:
    """The environment secret boundary is only as strong as this policy: it
    must name exactly ``main``, never the "protected branches" fallback
    (which admits every branch GitHub currently marks protected — a no-op
    boundary on a repository with no actually-protected branches) and never
    a wildcard or additional branch/tag pattern.
    """
    if audit_result.protected_branches_fallback:
        raise CandidateCiSetupError(
            f"environment {audit_result.environment!r} uses the 'protected branches' deployment "
            f"policy fallback — this must be an explicit custom policy naming exactly 'main', "
            f"not a fallback that silently tracks whatever branch protection happens to be"
        )
    if not audit_result.custom_branch_policies:
        raise CandidateCiSetupError(
            f"environment {audit_result.environment!r} has no deployment branch policy configured — "
            f"any branch (including a candidate's own) could satisfy it"
        )
    if audit_result.branch_policy_names != ("main",):
        raise CandidateCiSetupError(
            f"environment {audit_result.environment!r} branch policy is "
            f"{audit_result.branch_policy_names!r}, not exactly ('main',)"
        )


def _main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", help="OWNER/REPOSITORY to audit")
    parser.add_argument("--require-app-scoped-gate", action="store_true")
    parser.add_argument("--check-name", default="candidate-verification")
    parser.add_argument("--trusted-app-id", type=int, default=None)
    parser.add_argument("--require-environment-locked-to-main", action="store_true")
    parser.add_argument("--environment-name", default="candidate-verification-publish")
    args = parser.parse_args(argv)
    try:
        result = audit(args.repository)
        if args.require_app_scoped_gate:
            if args.trusted_app_id is None:
                raise CandidateCiSetupError("--require-app-scoped-gate needs --trusted-app-id")
            require_app_scoped_gate(result, context=args.check_name, trusted_app_id=args.trusted_app_id)
        environment_result = None
        if args.require_environment_locked_to_main:
            environment_result = audit_environment(args.repository, args.environment_name)
            require_environment_locked_to_main(environment_result)
    except CandidateCiSetupError as exc:
        print(f"candidate CI setup blocked: {exc}", file=sys.stderr)
        return 1
    output = {
        "repository": result.repository,
        "classic_contexts": result.classic_contexts,
        "enforce_admins": result.enforce_admins,
        "check_app_ids": result.check_app_ids,
    }
    if environment_result is not None:
        output["environment"] = {
            "name": environment_result.environment,
            "protected_branches_fallback": environment_result.protected_branches_fallback,
            "custom_branch_policies": environment_result.custom_branch_policies,
            "branch_policy_names": environment_result.branch_policy_names,
        }
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
