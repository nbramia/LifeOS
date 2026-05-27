# Launchd Setup — Superseded

**Status:** Superseded
**Last Updated:** 2026-05-27
**Audience:** Operators

> **This guide is superseded by [ADR-007: Linux Migration](../adr/007-linux-migration.md).**
>
> LifeOS no longer ships a macOS-as-primary-server deployment. The Linux migration (ADR-007) moved the API server, ChromaDB, embedding pipeline, sync, agent worker, and LLM orchestration to a Linux workstation under **systemd** (`./scripts/setup-systemd.sh`).
>
> The original content of this guide (`com.lifeos.api`, `com.lifeos.crm-sync`, `com.lifeos.chromadb` launchd plists and the ChromaDB cron watchdog) is preserved in git history if you need it for a legacy Mac Mini deployment.

## If you have a Mac in the system today

It's almost certainly running the **Apple Data Agent** — a nightly export of iMessage / contacts / call history / Photos face data that rsyncs to the Linux server. The design rationale is in [ADR-010: Apple Data Agent](../adr/010-apple-data-agent.md); the operator setup steps are in [installation.md § Step "FDA wrapper"](installation.md) and [setup.md § "Phase 9: FDA Wrapper (macOS Apple Data Agent only)"](setup.md).

The Apple Data Agent uses **cron** (not launchd) — `cron` invokes the FDA-granted `/Applications/LifeOS.app` wrapper, which inherits Full Disk Access to read `~/Library/Messages/chat.db` etc. Cron is sufficient because the agent runs once per night; launchd's keepalive / scheduling features aren't useful for a single nightly job.

## Related Documents

- [ADR-007: Linux Migration](../adr/007-linux-migration.md) — Why LifeOS moved off launchd and what runs where now
- [ADR-010: Apple Data Agent](../adr/010-apple-data-agent.md) — The Mac's post-migration role: nightly source, FDA wrapper, rsync transport
- [Installation](installation.md) — Linux + Apple Data Agent setup walkthrough
- [Setup](setup.md) — Phase-by-phase setup; Phase 9 covers the macOS FDA wrapper
- [Configuration](configuration.md) — All env vars referenced by setup
