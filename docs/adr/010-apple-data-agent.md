# ADR-010: Apple Data Agent — Mac as Nightly Data Source, Linux as System of Record

**Status:** Complete
**Last Updated:** 2026-05-27
**Decision:** Accepted

## Context

[ADR-007](007-linux-migration.md) moved LifeOS's primary runtime from a Mac Mini to a Linux workstation. Most data sources (Gmail, Google Calendar, Slack, Obsidian vault, Monarch Money, etc.) are platform-portable and run cleanly on Linux. **Three sources are not:**

- **iMessage** — `~/Library/Messages/chat.db`, SQLite read directly off the Mac.
- **Phone / FaceTime call history** — `~/Library/Application Support/CallHistoryDB/CallHistory.storedata`.
- **Apple Contacts + Photos face data** — via `pyobjc` bindings to native AddressBook and Photos frameworks.

These exist only on macOS. They require:

1. **macOS Transparency, Consent, and Control (TCC) — Full Disk Access (FDA).** Reading the Messages SQLite, call history, and contacts databases is gated by FDA. Without it, reads silently return empty results or fail.
2. **A binary that holds the FDA grant.** TCC grants are per-binary, not per-user — they survive across invocations of the granted binary but don't extend to children spawned through `cron` or `launchd` from another binary.
3. **A persistent host.** Once granted, the FDA-holding binary needs to be reachable from `cron` / `launchd` so the export can run unattended.

LifeOS still wants this data — iMessage history is central to "who did I talk to about X?" queries. The choice is **how** to bridge a macOS-only data source to a Linux primary runtime.

## Decision

A dedicated Mac runs `/Applications/LifeOS.app` — a tiny bash-script-based `.app` bundle that holds Full Disk Access. The Mac runs a nightly cron job that:

1. Invokes `LifeOS exec` with the export script, so the work runs inside the FDA-granted binary.
2. Exports Apple-only data (iMessage SQLite copy, call history, Apple Contacts dump, Photos face data) to a local staging directory.
3. `rsync`s the staging directory over SSH (Tailscale-routed) to the Linux server.

The Linux server owns everything else:

- All long-lived SQLite tables (interactions, entities, person facts).
- ChromaDB collections (embeddings, vector search).
- The seven-phase nightly sync pipeline (Linux runs phase 5 *imports* the Mac's export, then proceeds to embed, resolve entities, etc.).
- The agent worker, chat orchestrator, MCP server.

The Mac is **stateless w.r.t. LifeOS** — losing the Mac's local cache loses nothing that matters. The Linux server is the only system of record.

### LifeOS.app

`/Applications/LifeOS.app/Contents/MacOS/LifeOS` is a bash script (~30 lines). It exists so the FDA grant is attached to a known, persistent path. Operators grant FDA to `/Applications/LifeOS.app` once in System Settings → Privacy & Security → Full Disk Access. After that, `LifeOS exec <script>` runs `<script>` as a child of the FDA-holding binary, inheriting the grant. The cron entry calls `LifeOS exec scripts/apple_data_agent.sh`.

### Transport: rsync over SSH

The export pushes via `rsync -e ssh` to the Linux server. Tailscale provides the network — both machines are on the same tailnet. SSH key auth (no passwords). rsync's `--delete` is **not** used (the Linux side may have additional data from other sources). Each export overwrites the previous staging snapshot.

## Rationale

- **TCC can't be bypassed.** macOS FDA exists to gate access to user data. Disabling or circumventing it would be a security regression. The `.app` bundle is the supported pattern for holding a persistent FDA grant.
- **FDA grant persistence requires a wrapper binary.** `cron` and `launchd` jobs without FDA can't read `~/Library/Messages/`. Routing through `LifeOS exec` is the simplest way to give cron-triggered scripts FDA inheritance.
- **rsync is the right transport.** Idempotent, well-understood, survives Mac sleep cycles (resumes on next run), no schema-on-the-wire to drift. The alternative protocols (HTTP, sync frameworks) add code without solving a problem rsync hasn't.
- **Separating Mac (data source) from Linux (system of record) means the Mac can fail without taking down LifeOS.** Search, chat, agent worker all keep working with stale Apple data. The Mac's role is narrow — export and rsync — and its failure is a freshness problem, not an availability problem.
- **FDA scope is minimized.** The `.app` is a bash script, not the full Python project. Only that binary holds FDA; the rest of the LifeOS codebase doesn't need it.

## Alternatives Considered

### Run LifeOS entirely on the Mac

Skip the Linux migration and keep everything on a Mac (likely a Mac Studio with high RAM).

**Rejected because:** GPU and RAM constraints. A 96GB-VRAM AMD GPU enables local LLM orchestration ([ADR-007](007-linux-migration.md)) — Apple Silicon doesn't offer the same VRAM headroom for the same budget, and the ROCm ecosystem doesn't run on macOS. Also creates an SPOF: every LifeOS component depends on one Mac.

### Third-party Apple data extractor (e.g., iMazing CLI)

Use a commercial Mac-data-extraction tool (iMazing, etc.) to handle the export, then rsync its output to Linux.

**Rejected because:** Privacy + control. iMazing reads the same files but adds a third-party dependency for personal data. Lifecycle risk — if the tool changes formats or drops Mac-side support, LifeOS breaks. The 660-line `apple_data_export.py` is straightforward enough to maintain in-tree.

### Pull from iCloud APIs

Pull iMessage history, contacts, and call history from iCloud rather than off the Mac.

**Rejected because:** Several problems. Apple doesn't expose iMessage history via a developer API. Contacts are available via CloudKit but require Apple Developer membership and per-app data-handling review. The data on iCloud is also incomplete (only the most recent slice of messages depending on iCloud Messages settings). And pulling via cloud APIs gives up the local-first principle that motivates LifeOS in the first place.

### Push from Mac via network protocol (HTTP, sync framework)

Build a small HTTP service on the Mac that pushes new rows to the Linux server.

**Rejected because:** Adds two failure modes (the Mac-side server, the wire protocol) that rsync doesn't have. Rsync handles partial transfers, interrupted networks, and Mac sleep cycles automatically. A custom protocol would have to re-solve all of that. The wire schema would also become a maintenance surface — every new column in the iMessage export would need protocol-level handling. rsync just copies files.

### Mount the Mac's filesystem on Linux (SSHFS, NFS, etc.)

Skip the export step entirely; have Linux read the Mac's `~/Library/` over a network filesystem.

**Rejected because:** TCC doesn't grant FDA across network mounts in a way that makes the Messages SQLite readable. Even if it did, holding open a SQLite database across a network mount is a recipe for corruption. The export-to-snapshot pattern decouples the read (on Mac, with FDA) from the consumption (on Linux, against a stable file).

## Consequences

### Positive

- Clean machine separation: Mac fails → freshness degrades, but search/agent/chat all keep working.
- Minimal moving parts on the Mac (one cron entry, one `.app`, one rsync push).
- FDA scope is limited to one binary; the rest of LifeOS doesn't need it.
- Stateless Mac means rebuilding the Mac side (new hardware, OS reinstall) needs only to grant FDA + drop in `apple_data_agent.sh`.
- rsync is well-understood and easy to debug (`--dry-run`, `--itemize-changes`).

### Negative

- Two-machine setup complexity. Operators who don't have a Mac handy can't ingest iMessage / contacts / call history.
- macOS TCC reset (system update, app re-signing, `tccutil reset SystemPolicyAllFiles`) silently breaks exports until FDA is re-granted. There's no programmatic way to detect "FDA was revoked" from inside the exporting script — only `chat.db` returning empty data, which looks like "no new messages."
- rsync has no schema validation. A corrupt or partial export file reaches the Linux importer, which has to defend against it.
- Operator owns two machines now: keeping the Mac powered on, on the tailnet, and with the LifeOS.app FDA grant intact is part of the operational burden.
- Network coupling: if Tailscale is down or the Mac is offline at nightly-sync time, the Apple data sources are stale. The Linux sync still runs but skips imports.

### Failure modes

- **Mac offline at nightly sync time** → import step skips; iMessage/contacts/calls go stale; search/chat keep working with previous data. Staleness is detectable via export file mtime.
- **FDA grant revoked** → export script reads empty databases; produces zero-row exports. Looks like "no new data" rather than an error. Mitigation: the importer logs row deltas; a sudden drop to zero is a flag.
- **rsync partial / interrupted** → next run resumes (rsync's default behavior). Linux importer is idempotent and re-processable from any partial state.
- **Mac sleep / power event** → cron jobs missed; data is at most one day stale by the next run. Acceptable for the freshness expectations of personal data.

## Related Documents

### Design Context
- [ADR-007: Linux Migration](007-linux-migration.md) — Established the multi-machine pattern this ADR formalizes; the Mac's role change to "Apple Data Agent" was part of that decision but not given its own record
- [ADR-014: Apple Health collection & import](014-apple-health-collection.md) — Parallels this pipeline for Health data; explains why the Mac-side reader doesn't extend to HealthKit (iOS-only data)

### Specifications
- [Data & Sync](../specs/technical/data-and-sync.md) — Seven-phase nightly sync pipeline; phase 5 imports the Mac's export

### Operational
- [Installation Guide](../guides/installation.md) — Apple Data Agent setup steps (FDA grant, cron entry, Tailscale)
- [launchd Setup](../guides/launchd-setup.md) — macOS launchd configuration that's now Apple-Data-Agent-only

### Code References
- [`scripts/apple_data_export.py`](../../scripts/apple_data_export.py) — Reads iMessage SQLite, call history, contacts, photos face data; writes staging snapshot (660 lines)
- [`scripts/apple_data_import.py`](../../scripts/apple_data_import.py) — Linux-side importer that consumes the rsynced snapshot and writes to LifeOS SQLite (665 lines)
- [`scripts/apple_data_agent.sh`](../../scripts/apple_data_agent.sh) — Mac cron wrapper: invokes `LifeOS exec` so the export runs with FDA, then rsyncs to the Linux server (184 lines)
