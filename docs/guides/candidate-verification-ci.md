# Candidate Verification CI

**Status:** Partial
**Last Updated:** 2026-09-12
**Audience:** Operators

Candidate verification runs on GitHub-hosted ephemeral runners. The publisher
passes the protected base commit SHA it observed while building the candidate;
the job separately checks out that exact runner SHA and the exact candidate
SHA, proves both checkout identities, and disables persisted credentials. It uses only
`contents: read`, invokes the base revision's verifier against the candidate
checkout, creates a synthetic runtime home through that verifier, and runs
the existing fast-unit plus server-free-browser lanes. It installs
dependencies from the candidate's `requirements.txt` under a CI-only exact
CPU torch constraint. CI installs that pinned CPU-only torch wheel from the
CPU index first, then resolves the complete unmodified candidate requirement
file against the same constraint; a candidate that declares an incompatible
torch version fails resolution instead of being silently rewritten. It installs Chromium through the
declared Playwright dependency, verifies the installed torch version is the
expected `+cpu` build with no CUDA or HIP build metadata, and records a
sorted installed-package fingerprint in the ephemeral runner output.

The `local` runner's `--parallel-browser-free` flag opts the browser-free
lane into `pytest-xdist` at `--workers`' worker count (`browser-free`
excludes `requires_server`, so independent workers never share a live
server). It defaults off, and this CI job does not pass it, so CI's
browser-free lane stays serial.

`nbramia/LifeOS` is a User-owned repository, not an organization. GitHub
merge queue and Enterprise "required workflow" repository rules are both
organization/Enterprise-only features — not merely unconfigured, but
structurally unavailable here regardless of settings changes. Neither is
part of this design.

## Partitioning a lane into parts

`--part-index`/`--part-count` split the selected lanes into **parts** on both
the `pushed-ref` and `local` runners, so several concurrent runs can each
verify one share of the same candidate. `--shard-index`/`--shard-count`
partition by individual node ID instead; the two are alternatives and
requesting both is rejected.

A part is a set of **whole module scopes**, never a fraction of one. That unit
is load-bearing rather than convenient. `tests/conftest.py` skips
conditionally — an unavailable embedding service, a locked database — and a
test separated from state its own module established takes those paths. The
verifier requires every requested node ID to report `passed`, permitting only
the privacy audit as a skip, so one extra conditional skip fails the lane even
though that part's own pytest exited zero with everything it ran green.
Assigning whole modules keeps every test with its module's state, which is
also the grouping `--dist loadscope` already relies on inside a single part.
Scopes are assigned across every requested lane at once, so a module whose
tests span two lanes still lands wholly in one part.

Parts are balanced by **measured duration**, not test count: equal test counts
produce wildly unequal wall clocks. `scripts/lane_scope_durations.json` records
seconds per module, read from the *runner's* own checkout so every part of one
candidate derives its assignment from byte-identical input. Assignment is
longest-first: scopes descend by recorded cost, ties broken by name, and each
goes to the part carrying the least so far. A module with no recorded duration
is estimated from the recorded mean seconds per test, which degenerates to
balancing by test count when the record is absent or unreadable — correct, just
less balanced.

Refresh the record from the lane-execution receipts a completed run leaves in
its `--lane-log-dir`, passing that flag once per directory to merge every part
of a partitioned run:

```bash
~/.venvs/lifeos/bin/python scripts/verify_candidate.py record-scope-durations \
  --lane-log-dir /path/to/lane-logs
```

### What still blocks running the gate in parts

The hosted gate runs one execution job, not a matrix, because the `fast-unit`
lane is not yet safe to partition at all. At least one module —
`tests/test_briefings_service.py` — takes `require_db`'s conditional skip for
five of its seven tests when it runs without whichever other module first
creates the runtime `interactions` table, which it does even when that module
is the only one selected. A whole-lane run happens to win that ordering; a
four-part run of the same candidate does not, and the part owning that module
correctly fails with five skips against the whole lane's one.

That is a cross-module prerequisite, which no partition of whole modules can
satisfy: such a module has to establish its own store. Until every module in
the lane is self-sufficient, enabling a matrix would fail the gate for every
candidate. Prove coverage equivalence — an unpartitioned run and a partitioned
run of the same candidate reporting the same skip count — before changing the
topology.

Note also that a workflow change cannot verify itself: `pull_request_target`
and `workflow_dispatch` both resolve the workflow definition from the base
branch, so a PR that edits the topology is gated by whatever topology `main`
carries, and an edited topology's first real exercise is the first candidate
built after that edit merges.

## Privacy-audit applicability

`tests/test_fixtures_no_personal_data.py::test_no_fixture_contains_a_real_sensitive_value`
compares committed fixtures with a locally reachable real `.env` without
loading it into the process environment. In an isolated candidate where that
file is intentionally inaccessible, only its exact existing no-real-`.env`
reason is recorded as named `not_applicable`; it is never reported as passed.
All other skipped, missing, errored, or failed tests remain blocking, and a
successful lane still requires at least one passed mandatory test; the direct
checkout/pre-push audit continues to run read-only whenever the `.env` exists.

## The actual gate: a dedicated App-issued required check

The publisher job is separate from candidate execution. It receives
`checks: write`, does not check out candidate files or consume candidate
artifacts, and reports the result for the event's exact candidate SHA. A
missing, cancelled, or failed execution maps to a failed aggregate result; no
docs-only path skips the aggregate job.

GitHub's required-status-check matching identifies a `context` string and a
reporting `app_id` — never which workflow file or run produced it. A
required check bound only to the generic GitHub Actions app (`app_id: 15368`)
or left unbound (`app_id: -1`, "any app") can be satisfied by any workflow in
the repository with `checks: write`, including one a candidate PR adds or
modifies itself, because a `pull_request`-triggered (not `_target`) workflow
resolves its own definition from the candidate's tree. Closing this requires
the required check to be bound to a **dedicated GitHub App** whose
installation token is minted only inside the `publish-aggregate` job (never
stored as a persisted secret used by any candidate-influenceable workflow,
and never given to the `execute-candidate` job at all).

A **repository secret is not sufficient to hold that App's private key**,
even though only `publish-aggregate` references it: a repository secret is
available to any same-repo, non-fork workflow run regardless of which
workflow file defines it, so a candidate-authored `pull_request`-triggered
workflow could add its own step referencing that same secret name and mint
a valid App token itself — a separate job in the checked-in workflow does
not create a boundary a candidate's own new workflow file can't cross. The
key must instead be an **environment secret**, scoped to a
`candidate-verification-publish` GitHub Environment whose **deployment
branch policy is configured to exactly `main`** (no tags, no `refs/pull/*`,
and not the "protected branches" fallback unless `main` is the only
protected branch — check the actual configured value, don't assume). Only a
job that declares `environment: candidate-verification-publish` can resolve
the secret, and GitHub denies that resolution unless the run ref matches the
policy — so a `pull_request`-triggered run (whose ref is the pull request
head/merge ref, never `main`) is refused the secret even if its own new
workflow file declares the same environment name. `execute-candidate` never
declares this environment.

There are two independent event paths in `.github/workflows/candidate-verification.yml`,
never conflated:

- **`pull_request_target`** publishes `candidate-verification-shadow` for a
  PR head. It is diagnostic — fast, visible PR feedback — and must never be
  a required check.
- **`workflow_dispatch`** (input: `candidate_sha`) verifies the exact
  normalized two-parent candidate `scripts/candidate_publisher.py`
  constructs, and publishes the actual required check, `candidate-verification`.
  Both event types resolve the workflow's own YAML from a trusted ref (the
  base branch for `pull_request_target`, the `--ref` the dispatch targets —
  always `main` — for `workflow_dispatch`), never from the candidate's own
  `.github/workflows/**`, which is what keeps a candidate-modified workflow
  file from ever running with elevated privilege.

For the authoritative dispatch, the publisher also supplies
`trusted_runner_sha`: the protected-base commit it observed before constructing
the candidate. The job verifies its `trusted-runner` checkout matches that
SHA, uses it as the verifier's base identity, and includes it in the aggregate
check summary. The authoritative dispatch additionally requires that SHA to
equal the immutable `github.workflow_sha` and the candidate's first parent;
a later advance of `main` therefore fails closed rather than silently
substituting a different runner implementation for the one claimed by that
check.

`scripts/candidate_publisher.py` orchestrates the actual publish: it builds
the candidate, pushes it to a disposable staging ref, dispatches
`workflow_dispatch` against it, polls for the resulting check via the
GitHub API filtered to the dedicated App's `app_id` (never accepting a
same-named check from any other app), and only then performs the atomic
`main`+source publish. Because a candidate SHA is deterministic from its
content, re-publishing an unchanged branch rebuilds the identical candidate,
which may already carry verdicts from an earlier dispatch; the publisher
snapshots those check-run ids before dispatching and accepts only a check this
invocation caused, taking the most recently started completed one rather than
whichever the API lists first. That holds in both directions — a stale failure
never refuses a candidate whose fresh run passed, and a stale success never
authorizes publication of one whose own run did not. See `scripts/candidate_publisher.py`'s module
docstring for the exact ordering guarantee (construct before verify, verify
before publish).

The production publisher rejects the generic Actions app (`15368`), the
wildcard issuer (`-1`), and every nonpositive app id without reading the pull request or
fetching any refs. The low-level check-polling helper can still be used by
disposable mechanics probes without an app filter, but that helper is not a
publication bypass: the production function and CLI always require a positive,
dedicated App id.

Before fetching and again immediately before the atomic push, publication
queries open PRs for another PR with the same head repository and branch via
GitHub's paginated REST endpoint. The target PR itself must appear with that
exact source identity before an empty conflict list is trusted. If one exists,
publication is refused because a single source-ref lease cannot
represent two open PRs safely. These API observations are point-in-time
exclusivity checks, not a GitHub metadata lock: a new PR can still be opened in
the final query-to-push interval, while the dual main/source SHA leases still
reject a source-content race. Stronger exclusion requires a branch policy or a
unique source-ref convention outside this publisher.

The atomic operation always advances the retained source ref to the exact
candidate SHA. With the default behavior, a later candidate-leased deletion
removes that source ref only after GitHub's native merged-PR bookkeeping has
recorded the merge; `--no-delete-source` skips that deletion and intentionally
leaves the source ref at the candidate SHA. It never restores the original PR
head, and a newer source commit causes the cleanup lease to retain the newer
ref.

## Enabling the Gate

1. Create the `candidate-verification-publish` Environment on the
   repository first, and set its deployment branch policy to exactly
   `main` — no tags, no `refs/pull/*`, and not the "protected branches"
   fallback unless `main` is genuinely the only branch marked protected.
   `python scripts/candidate_ci_setup.py OWNER/REPOSITORY
   --require-environment-locked-to-main` audits this.
2. Run `python scripts/github_app_setup.py OWNER/REPOSITORY`. This writes an
   auto-submitting manifest form to `github-app-manifest.html` (permissions:
   `checks: write` only, no webhook events) and starts a local callback
   listener. Open that file in an authenticated GitHub browser session and
   submit it — GitHub's App Manifest flow has no fully headless path at any
   permission level, so this one step needs an authenticated browser action;
   it does not need to be a specific human, only an authenticated session
   with the right access (an operator's own browser, or an authorized
   automated browser session acting on their behalf). The script then
   exchanges the resulting one-time code for real credentials, then checks
   whether the App is actually installed on the target repository using a
   short-lived App JWT signed in memory from the exchanged key, rather than
   the CLI user's OAuth installation endpoint. Registering an App and
   installing it are separate GitHub actions — a
   fresh App is ordinarily not installed yet). If it isn't, the script
   prints the installation URL and keeps running, polling until
   installation is confirmed — the exchanged private key exists only in
   this process's memory, so it deliberately waits rather than exiting and
   losing the only copy (which would force starting over from a fresh
   registration). Install the App on `nbramia/LifeOS` while it waits. Once
   installed, it confirms the destination environment's branch policy is
   genuinely locked to `main` before writing anything, and writes
   `LIFEOS_CANDIDATE_APP_ID` (variable) and
   `LIFEOS_CANDIDATE_APP_PRIVATE_KEY` (secret) directly into the
   `candidate-verification-publish` environment created in step 1 — never
   repository-level, and the private key is never printed, logged, or
   returned anywhere else in the process.
3. Configure branch protection's required status check for `main` to require
   `candidate-verification`, bound to that App's id.
4. Run the audited setup check before trusting that configuration:

```bash
python scripts/candidate_ci_setup.py OWNER/REPOSITORY \
    --require-app-scoped-gate --check-name candidate-verification \
    --trusted-app-id <the App's numeric id>
```

It deliberately fails when the configured required check is bound to the
generic Actions app, to the wildcard (`app_id: -1`), or to no app at all —
none of those identify a specific, non-candidate-controllable issuer.
The required check serves as the merge gate when this command succeeds and
administrator enforcement is enabled.

Until the audit and hosted enforcement exercise both succeed, retain the
local broad pre-push gate.

## Local cutover and rollback

Apply the local cutover only after all of these are current: the dedicated
App is installed, `main` requires `candidate-verification` from App
`4891159`, administrator enforcement is enabled, the environment policy is
exactly `main`, and an actual hosted candidate exercise has shown that a
passing check permits publication while an absent, failed, cancelled, stale,
or wrong-candidate check blocks it. The operator records the policy proof
with:

```bash
python scripts/candidate_ci_setup.py nbramia/LifeOS \
  --require-app-scoped-gate --check-name candidate-verification \
  --trusted-app-id 4891159 --require-environment-locked-to-main
```

After that proof, the tracked pre-push hook keeps a cheap local gate: Ruff
checks changed Python files and the direct, read-only fixture privacy audit.
The audit reports either a pass or its named no-real-`.env` not-applicable
result; any other result blocks the push. Its per-push log remains the local
evidence record, while the App-bound candidate receipt remains the merge
evidence; ordinary pushes do not rerun the broad fast-unit/browser lanes.

Before disabling the hosted requirement, first restore the existing blocking
local verifier in every checkout that may push:

```bash
./scripts/setup-hooks.sh --restore-blocking-local-verification
git config --get --bool lifeos.prepush.blocking-local-verification
```

The second command must print `true`; then make and retain one successful
blocking local verification before removing the required check. After hosted
protection is re-established and audited again, remove that local rollback
setting with `git config --unset-all lifeos.prepush.blocking-local-verification`.

## Related Documents

### Operational

- [Scripts Reference](scripts.md) — Local and remote test entry points retained during CI rollout.

### Code References

- [Candidate workflow](../../.github/workflows/candidate-verification.yml) — Hosted execution and aggregate publication boundary.
- [Setup audit](../../scripts/candidate_ci_setup.py) — Fail-closed app-scoped policy inspection.
- [Verifier](../../scripts/verify_candidate.py) — Exact-SHA lane collection and execution.
- [Publisher](../../scripts/candidate_publisher.py) — Construct-then-verify-then-publish orchestration.
- [App setup](../../scripts/github_app_setup.py) — One-time dedicated GitHub App registration and credential storage.
