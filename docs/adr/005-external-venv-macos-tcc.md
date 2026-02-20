# ADR-005: External Virtual Environment for macOS TCC

> **Decision:** Place the Python virtual environment at `~/.venvs/lifeos`, outside the project directory.
> **Date:** 2026-02-19
> **Status:** Accepted
> **Last Updated:** 2026-02-19

## Context

LifeOS runs as a launchd service on macOS. The project source code lives in `~/Documents/Code/LifeOS/`, which is synced via iCloud between the Mac Mini (server) and MacBook Pro (development machine). This syncing is valuable — code edits on the MacBook are immediately visible on the Mac Mini without manual file transfer.

macOS TCC (Transparency, Consent, and Control) performs security scanning on files within protected directories like `~/Documents/`. When launchd loads a Python process whose virtual environment is inside `~/Documents/`, TCC scans every `.pyc` and `.so` file in the venv — hundreds of files across dozens of packages. This caused server startup times of **30+ seconds**, unacceptable for a service that needs to restart quickly after code changes during development.

The issue is specific to launchd-triggered processes. Running the same venv from an interactive terminal session does not trigger the same scanning delay, which made this problem difficult to diagnose initially. The root cause was identified by profiling server startup and observing that file I/O to venv directories dominated the startup trace under launchd but not under interactive shells.

## Decision

Place the virtual environment at `~/.venvs/lifeos` (outside `~/Documents/`). All scripts reference this path explicitly. The project source code remains in `~/Documents/` for iCloud sync.

## Rationale

- **Performance**: `~/.venvs/` is not a TCC-protected directory. Server startup drops from 30+ seconds to under 2 seconds.
- **iCloud sync preserved**: Source code stays in `~/Documents/` and syncs between machines automatically. Only the venv (which is machine-specific anyway) moves out.
- **Minimal disruption**: The fix requires updating paths in scripts and documentation, not restructuring the project.
- **No security compromise**: The venv contains third-party packages, not sensitive data. Moving it outside TCC scanning does not reduce security posture.

## Alternatives Considered

### Disable TCC Scanning

macOS provides some mechanisms to exclude directories from TCC scanning, but these require modifying system security settings. Disabling or reducing TCC protection system-wide is a disproportionate response to a venv performance issue. TCC exists to protect user data from unauthorized access, and weakening it to solve a deployment convenience problem sets a poor precedent. The external venv approach achieves the same performance benefit without touching security settings.

### Move Entire Project Out of ~/Documents/

Moving the project to a non-TCC directory (e.g., `~/Code/` or `/opt/lifeos/`) would solve the TCC scanning issue for both source code and venv. However, this breaks iCloud sync between the Mac Mini and MacBook, which is fundamental to the development workflow — edits on the MacBook appear on the Mac Mini without SSH, git push, or rsync. Replacing iCloud sync with a manual mechanism (git-based workflow, rsync scripts, or Syncthing) adds friction to every development cycle. Moving only the venv out preserves the sync workflow while solving the performance problem.

### Use System Python

Using the macOS system Python (or Homebrew Python) without a virtual environment would avoid the venv scanning issue entirely. However, this eliminates dependency isolation. LifeOS has 50+ pinned dependencies, several of which conflict with versions required by other Python projects. Without a venv, installing LifeOS dependencies could break other tools on the system. Version pinning becomes fragile without the isolation boundary that a venv provides.

### Docker Container

Running LifeOS in a Docker container would isolate the entire runtime from TCC scanning. However, Docker Desktop on macOS introduces its own performance issues — filesystem I/O via VirtioFS adds measurable latency to every file operation, which is problematic for a system that reads and writes thousands of files during sync operations. Docker also adds significant operational complexity (container management, volume mounts, networking) for a single-user system where the simpler fix is moving a directory. The Docker overhead is not justified for this use case.

### Homebrew Python Without Venv

Similar to system Python, using Homebrew's Python without a venv removes the scanning target. But it shares the same isolation problems — no way to pin LifeOS-specific dependency versions without risking conflicts with other Python tools installed via Homebrew. The Homebrew Python installation is also a moving target that updates independently, which can break pinned dependencies.

## Consequences

**Positive:**
- Server startup drops from 30+ seconds to under 2 seconds.
- iCloud sync continues working for source code.
- No security compromise — only third-party packages move outside TCC scope.

**Negative:**
- Virtual environment is not co-located with the project, which is confusing for initial setup. Requires explicit documentation.
- All scripts must use absolute paths to the venv (`~/.venvs/lifeos/bin/python`).
- The venv exists only on the Mac Mini. The MacBook development machine does not have one — tests and server commands must be run remotely via SSH.
- `pip install` must be run on the Mac Mini, not the MacBook.

**Risks:**
- The separated venv path is a constant source of confusion for new agents and tools that expect `./venv/` or `.venv/` in the project root. Documentation must be explicit and scripts must use absolute paths consistently.
- If iCloud sync is replaced with a different mechanism in the future, the motivation for keeping source code in `~/Documents/` weakens, and the project layout should be re-evaluated.
- macOS TCC behavior may change in future versions, potentially making this workaround unnecessary — or introducing new scanning patterns that affect `~/.venvs/`.

## Related Documents

**Design Context:**
- [ADR-001: Python/FastAPI](001-python-fastapi.md) — The Python stack that requires a venv

**Specifications:**
- [Architecture](../specs/technical/architecture.md) — System architecture including deployment model

**Operational:**
- [Installation Guide](../guides/installation.md) — Setup instructions including venv creation
- [launchd Setup](../guides/launchd-setup.md) — Service configuration that's affected by TCC
- [Scripts Reference](../guides/scripts.md) — Scripts that reference the external venv path
