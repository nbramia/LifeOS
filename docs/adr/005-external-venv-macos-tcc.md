# ADR-005: External Virtual Environment for macOS TCC

**Status:** Complete
**Last Updated:** 2026-03-04
**Decision:** Superseded
**Superseded By:** ADR-007

> The original TCC-driven rationale no longer applies after the Linux migration (see [ADR-007](007-linux-migration.md)). The practice — venv at `~/.venvs/lifeos`, outside the project directory — is retained by convention on both Linux and macOS, but for different reasons (keeping the venv out of file-sync tools and out of the project tree). This ADR is preserved as the original record.

## Context

LifeOS originally ran as a launchd service on macOS. The project source lived in `~/Documents/Code/LifeOS/`, synced via iCloud between the Mac Mini (server) and MacBook Pro (development machine). Code edits on the MacBook were immediately visible on the Mac Mini without manual file transfer.

macOS TCC (Transparency, Consent, and Control) performs security scanning on files within protected directories like `~/Documents/`. When launchd loaded a Python process whose virtual environment was inside `~/Documents/`, TCC scanned every `.pyc` and `.so` file in the venv — hundreds of files across dozens of packages. This caused server startup times of **30+ seconds**, unacceptable for a service that needed to restart quickly after code changes.

The issue was specific to launchd-triggered processes. Running the same venv from an interactive terminal session did not trigger the same scanning delay, which made the problem difficult to diagnose initially. The root cause was identified by profiling server startup and observing that file I/O to venv directories dominated the startup trace under launchd but not under interactive shells.

## Decision

Place the virtual environment at `~/.venvs/lifeos` (outside `~/Documents/`). All scripts reference this path explicitly. The project source code remains in `~/Documents/` for iCloud sync.

## Rationale

- **Performance**: `~/.venvs/` is not a TCC-protected directory. Server startup drops from 30+ seconds to under 2 seconds.
- **iCloud sync preserved**: Source code stays in `~/Documents/` and syncs between machines automatically. Only the venv (which is machine-specific anyway) moves out.
- **Minimal disruption**: The fix requires updating paths in scripts and documentation, not restructuring the project.
- **No security compromise**: The venv contains third-party packages, not sensitive data. Moving it outside TCC scanning does not reduce security posture.

## Alternatives Considered

### Disable TCC Scanning

macOS provides some mechanisms to exclude directories from TCC scanning.

**Rejected because:** Doing so requires modifying system security settings. Disabling or reducing TCC protection system-wide is a disproportionate response to a venv performance issue — TCC exists to protect user data from unauthorized access, and weakening it to solve a deployment convenience problem sets a poor precedent. The external venv achieves the same performance benefit without touching security settings.

### Move Entire Project Out of `~/Documents/`

Move the project to a non-TCC directory (e.g., `~/Code/` or `/opt/lifeos/`).

**Rejected because:** This breaks iCloud sync between the Mac Mini and MacBook, which is fundamental to the development workflow — edits on the MacBook appear on the Mac Mini without SSH, git push, or rsync. Replacing iCloud sync with a manual mechanism (git-based workflow, rsync scripts, Syncthing) adds friction to every development cycle. Moving only the venv out preserves the sync workflow while solving the performance problem.

### Use System Python

Use the macOS system Python (or Homebrew Python) without a virtual environment.

**Rejected because:** This eliminates dependency isolation. LifeOS has 50+ pinned dependencies, several of which conflict with versions required by other Python projects. Without a venv, installing LifeOS dependencies could break other tools on the system. Version pinning becomes fragile without the isolation boundary a venv provides.

### Docker Container

Run LifeOS in a Docker container to isolate the entire runtime from TCC scanning.

**Rejected because:** Docker Desktop on macOS introduces its own performance issues — filesystem I/O via VirtioFS adds measurable latency to every file operation, problematic for a system that reads and writes thousands of files during sync operations. Docker also adds significant operational complexity (container management, volume mounts, networking) for a single-user system where the simpler fix is moving a directory.

### Homebrew Python Without Venv

Similar to system Python: use Homebrew's Python directly.

**Rejected because:** It shares the same isolation problems — no way to pin LifeOS-specific dependency versions without risking conflicts with other Python tools installed via Homebrew. The Homebrew Python installation is also a moving target that updates independently, which can break pinned dependencies.

## Consequences

### Positive

- Server startup drops from 30+ seconds to under 2 seconds.
- iCloud sync continues working for source code.
- No security compromise — only third-party packages move outside TCC scope.

### Negative

- Virtual environment is not co-located with the project, which is confusing for initial setup. Requires explicit documentation.
- All scripts must use absolute paths to the venv (`~/.venvs/lifeos/bin/python`).
- (Original era) The venv exists only on the Mac Mini; the MacBook development machine does not have one — tests and server commands must be run remotely via SSH.
- The separated venv path is a constant source of confusion for new agents and tools that expect `./venv/` or `.venv/` in the project root. Documentation must be explicit and scripts must use absolute paths consistently.
- macOS TCC behavior may change in future versions, potentially making this workaround unnecessary — or introducing new scanning patterns that affect `~/.venvs/`.

## Related Documents

### Design Context
- [ADR-001: Python/FastAPI](001-python-fastapi.md) — The Python stack that requires a venv
- [ADR-007: Linux Migration](007-linux-migration.md) — Supersedes this ADR; venv convention retained on Linux for different reasons

### Specifications
- [Architecture](../specs/technical/architecture.md) — System architecture including deployment model

### Operational
- [Installation Guide](../guides/installation.md) — Setup instructions including venv creation
- [launchd Setup](../guides/launchd-setup.md) — Service configuration that's affected by TCC (now Apple-Data-Agent-only after Linux migration)
- [Scripts Reference](../guides/scripts.md) — Scripts that reference the external venv path
