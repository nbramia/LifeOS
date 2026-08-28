# ADR-022: macOS FDA Inheritance (`run`, Not `exec`) and Safe Service Restart

**Status:** Complete
**Last Updated:** 2026-08-27
**Decision:** Accepted
**Amends:** [ADR-010](010-apple-data-agent.md) — corrects its `exec`/`run` description and adds two restart-safety facts ADR-010 never covered

## Context

A second operator's macOS deployment hit three problems in a row that existed only as tribal knowledge from debugging the first (Nathan's own) deployment — none of it was written down anywhere a fresh operator, or a Claude Code session helping one, would read before hitting the same wall:

1. **Force-restarting the API service via launchd's `kickstart` mechanism has wedged it before.** `kickstart -k` is meant to force-restart a *running* job in place; observed behavior on this project's setup is that it can leave the service in a half-started state rather than cleanly replacing the old process. A full stop of the old process, followed by a fresh start, avoids this — `scripts/service.sh`'s `stop_macos`/`start_macos` (`:127-155`) are already the safe primitives for this, but the guide never says "use these instead of kickstart" or why.
2. **The step that (re)loads the FDA-holding wrapper into launchd intermittently fails with a bare `Input/output error`.** This is a known-transient launchd/TCC hiccup, not a sign of misconfiguration — retrying the identical command succeeds. An operator hitting this cold, with no doc saying "this is expected, just retry," reasonably assumes something is broken and starts second-guessing the FDA grant, the plist, or the wrapper script instead.
3. **FDA inheritance follows the *responsible* process, not the running binary — and the docs had the mechanism backward.** macOS TCC attributes Full Disk Access to whichever process is recorded as "responsible" for a child (normally its parent), not to whichever binary is actually executing at a given moment. `/Applications/LifeOS.app` (see [ADR-010](010-apple-data-agent.md)) exists specifically to hold a persistent FDA grant that cron/launchd jobs can inherit — but which of its two invocation styles actually preserves that inheritance was documented backward. `scripts/create-lifeos-app.sh`'s `exec` case (`:152-156`) replaces the LifeOS.app process image with the invoked command via shell `exec` — the invoked command is no longer a *child* of LifeOS.app at all, it *is* the process, wearing a different binary; TCC's responsible-process chain is broken, and FDA is silently lost (the child just reads empty or protected data, with no error). The `run` case (`:157-161`) instead runs the command as an actual subprocess with LifeOS.app's script remaining the live parent, so TCC's responsible-process attribution still resolves to the FDA-granted bundle. Only the real, working export path (`scripts/apple_data_agent.sh:184-191`) has ever used `run`. **[ADR-010](010-apple-data-agent.md) itself, and `docs/guides/operations.md`'s FDA section, both described `exec` as the FDA-preserving call** — the opposite of what the code does and the opposite of what actually works. An earlier build of the wrapper offered only the `exec` style and, as a result, silently exported nothing.

None of this is capturable as a code change — the working code (`create-lifeos-app.sh`, `apple_data_agent.sh`) was already correct; only the docs, and an unwritten restart procedure, were wrong or missing. [ADR-010](010-apple-data-agent.md) is immutable per this project's ADR conventions, so its incorrect `exec` framing can't be edited in place; this ADR amends it with the corrected facts and the two additional restart lessons ADR-010 never addressed.

## Decision

Three operational rules govern macOS operation of the Apple Data Agent / LifeOS.app going forward:

1. **Never force-restart the API service (or anything routed through LifeOS.app) with `launchctl kickstart`.** Do a clean stop-then-start instead: `./scripts/service.sh stop` followed by `./scripts/service.sh start` (i.e. `stop_macos`/`start_macos`, `scripts/service.sh:127-155`) for an already-installed service; for a harder reset, a full teardown-then-bootstrap — `./scripts/service.sh uninstall` followed by `./scripts/service.sh install` — replaces the launchd job outright rather than asking launchd to restart a live one in place.
2. **A bare `Input/output error` from the bootstrap/load step is transient — retry it, don't treat it as a real failure.** This has been observed when (re)loading a launchd job or invoking the FDA-holding wrapper shortly after a stop/teardown; the identical command succeeds on retry. Only escalate (check the plist, check the FDA grant in System Settings) if it still fails after a retry or two.
3. **`LifeOS run`, not `LifeOS exec`, is what preserves Full Disk Access — and the grant belongs on the `.app` bundle, never on a shell or interpreter.** Route any new cron job or script that needs protected-directory access (Messages, Photos, Contacts, CallHistory databases) through `LifeOS.app`'s `run` subcommand, exactly as `scripts/apple_data_agent.sh:184-191` already does for the working export. `exec` looks equivalent and is offered by the same wrapper, but silently drops the grant — do not use it for anything that touches protected data. The FDA grant itself must be added to `/Applications/LifeOS.app` in System Settings → Privacy & Security → Full Disk Access — never to `/bin/bash`, `/bin/zsh`, or the Python interpreter it eventually invokes, since TCC grants are per-bundle and neither a shell nor a venv's `python3` has a stable enough identity to hold a persistent grant on.

## Rationale

- **The restart and retry lessons are field-observed, not derivable from any existing code or log** — the ad hoc script that hit both was never committed (tracked separately as the auto-update/launchd-packaging follow-up). Writing them down here is strictly better than leaving a second operator to rediscover them the same way, even without a code citation to point at.
- **The `run`/`exec` correction is derivable from code that was already right** — `create-lifeos-app.sh`'s own comment on the `run` case (`:159-161`) already explains the responsible-process mechanism correctly; the bug was purely that ADR-010's prose and `operations.md` described the *other* case as the safe one. Fixing the docs to match the code, rather than changing the code to match the (wrong) docs, is the only sound direction — the code is what's actually been exporting data successfully.
- **Amending rather than superseding ADR-010** is the correct move per this project's own ADR conventions (`docs/adr/AGENTS.md`): the underlying architecture decision — a `.app` wrapper holding a persistent FDA grant, invoked by cron/launchd — is unchanged and still correct. Only one factual detail in its description was backward, plus two operational facts it never covered. A full supersession would incorrectly imply the design itself is being replaced.

## Alternatives Considered

### Use `launchctl kickstart -k` for restarts, since it's the "standard" force-restart primitive

`kickstart -k` is the documented launchd mechanism for restarting a running job without unloading/reloading its plist, and is simpler to invoke than a stop/start or teardown/bootstrap pair.

**Rejected because:** field observation on this project's setup is that it can wedge the service rather than cleanly restarting it — the opposite of what a restart primitive is supposed to guarantee. `scripts/service.sh` already exposes a clean stop-then-start pair; there's no reason to reach for the riskier primitive when a safe one already exists in the codebase.

### Grant Full Disk Access directly to the shell or to the venv's `python3` interpreter

Skip the `.app` wrapper indirection and grant FDA straight to `/bin/bash` (or `~/.venvs/lifeos/bin/python3`), since that's the process actually reading the protected databases.

**Rejected because:** TCC grants are tied to a specific binary's identity, not to "whatever a script happens to invoke." A bare shell is used for far more than this one export, so granting it FDA is a much broader, harder-to-reason-about surface than granting one purpose-built `.app`. A venv's `python3` binary is also not a stable target — recreating the venv (a version bump, a fresh clone) changes the binary that would need the grant, silently breaking the export until someone remembers to re-grant it. The wrapper app's whole purpose (ADR-010) is to give the grant a fixed, narrow, persistent home; granting it to a shell or interpreter defeats that.

### Keep `exec` as the primary invocation style and fix `run` instead, or offer only one style

Since `exec` was the one documented (incorrectly) as correct, consider making it actually work instead of correcting the docs to point at `run`.

**Rejected because:** `exec`'s process-replacement semantics are fundamental to what `exec` *is* in a shell — there is no way to make a shell `exec` preserve a separate parent process, because after `exec` there is no separate parent process anymore. The only way to keep LifeOS.app as the responsible parent is to run the child as an actual subprocess, which is exactly what the `run` case already does correctly. Offering only `run` (dropping `exec` from the wrapper entirely) was considered but rejected as out of scope here — `exec` remains useful for wrapper commands that don't touch protected data and want to avoid an extra process in the tree; the fix is in the choosing, and in the docs guiding that choice, not in removing the option.

## Consequences

### Positive

- A future operator (or an agent debugging on their behalf) hitting the kickstart wedge, the bootstrap I/O error, or a silently-empty export now has a doc to check before spending time debugging the wrong layer.
- Code and docs now agree: `apple_data_agent.sh`'s actual `run` call, `create-lifeos-app.sh`'s own comment, and the docs all describe the same mechanism the same way.
- ADR-010's core architecture decision is preserved and clarified rather than replaced, keeping its history intact per this project's append-only ADR convention.

### Negative

- The kickstart-wedge and bootstrap-I/O-error lessons are anecdotal field observations without a precise root cause or a reproducing test — if the true underlying launchd/TCC behavior is later characterized more precisely (or found to depend on macOS version), this ADR's guidance should be tightened via a further amendment rather than assumed to be the full story.
- Nothing in this ADR adds automated enforcement that a future doc edit can't reintroduce the `exec`/`run` mix-up again; it remains a manual-review concern, same as any other doc/code consistency issue.

## Related Documents

### Design Context
- [ADR-010: Apple Data Agent](010-apple-data-agent.md) — The `.app` wrapper design this ADR amends; its `exec`/`run` framing was backward and its restart behavior was never addressed

### Operational
- [Operations Reference](../guides/operations.md) — macOS FDA section this ADR's corrections and additions are folded into
- [launchd Setup](../guides/launchd-setup.md) — macOS launchd service configuration for the API and Apple Data Agent

### Code References
- [`scripts/create-lifeos-app.sh:152-161`](../../scripts/create-lifeos-app.sh) — The wrapper's `exec` (loses FDA) and `run` (preserves FDA) subcommands, with the `run` case's own comment already explaining why
- [`scripts/apple_data_agent.sh:184-191`](../../scripts/apple_data_agent.sh) — The working export path; the only caller that has always used `run`
- [`scripts/service.sh:127-155`](../../scripts/service.sh) — `stop_macos`/`start_macos`, the clean-restart primitives to use instead of `kickstart`
