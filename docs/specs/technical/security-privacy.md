# Security & Privacy

> **Status:** Draft
> **Owner:** Core
> **Last Updated:** 2026-02-19

## Overview

LifeOS is single-user, self-hosted on macOS. The threat model is fundamentally different from multi-tenant systems. There are no user accounts, no access control lists, no role-based permissions. The primary security concern is preventing data leakage beyond the local machine and ensuring that sensitive personal data is handled with appropriate care in code, logs, and API interactions.

## Guiding Principles

- **All data stays local.** No cloud storage, no telemetry, no analytics. Data leaves the machine only as discrete query payloads to Claude API.
- **Claude API receives only query payloads.** The API call sends the user's question and relevant search results, never bulk data exports or full database dumps.
- **External service credentials are stored locally.** OAuth tokens, API keys, and session cookies never leave the machine.
- **Logs never contain personal data content.** Log entity IDs and counts, not message bodies, email content, or contact details.

## Data Boundaries

### What Stays Local

| Data Type | Storage Location |
|-----------|-----------------|
| Indexed content (notes, emails, messages) | ChromaDB vectors + SQLite metadata |
| CRM data (people, relationships, facts) | SQLite (`data/crm.db`) |
| Search indexes (BM25) | SQLite FTS5 tables |
| Sync state and job queue | SQLite (`data/jobs.db`) |
| OAuth tokens and API keys | `.env` file and service-specific files |
| Monarch Money session | `data/monarch_session.pickle` |
| Performance traces | SQLite (`data/perf_traces.db`) |
| Photos and media metadata | SQLite, local filesystem |

### What Leaves the Machine

| Data | Destination | Purpose | Frequency |
|------|-------------|---------|-----------|
| Query text + search result snippets | Claude API (Anthropic) | Chat synthesis | Per user query |
| Query text | Ollama (local process) | Query routing/classification | Per user query |
| OAuth authentication flows | Google, Slack | Token refresh | Periodic |
| Reminder messages | Telegram Bot API | Notification delivery | Per reminder |

## Credential Storage

| Credential | Location | Format |
|-----------|----------|--------|
| Anthropic API key | `.env` | `ANTHROPIC_API_KEY=sk-...` |
| Google OAuth tokens | `.env` + token files | OAuth2 refresh tokens |
| Slack tokens | `.env` | `SLACK_BOT_TOKEN=xoxb-...` |
| Telegram bot token | `.env` | `telegram_bot_token=...` |
| Monarch Money session | `data/monarch_session.pickle` | Pickle serialized session |

**Encryption at rest**: None beyond macOS FileVault (full-disk encryption). Credentials are stored in plaintext in `.env` and data files. This is acceptable for a single-user system with FileVault enabled — the threat model does not include local attackers with disk access, as that would imply full system compromise.

## API Security

- **No authentication on local API.** The FastAPI server runs on `0.0.0.0:8000` with no authentication. This is intentional for a single-user system.
- **Network access via Tailscale only.** Remote access from the MacBook to the Mac Mini uses Tailscale (WireGuard-based VPN). No ports are exposed to the public internet.
- **No public-facing endpoints.** The server is not accessible from outside the Tailscale network.

## Data Deletion

- **ChromaDB**: Documents can be deleted from collections by ID. Deletion removes both the vector and metadata.
- **SQLite databases**: Records can be deleted via SQL. WAL mode means deletions are journaled before being applied.
- **BM25 index**: FTS5 entries are deleted alongside their source records in SQLite.
- **No global "delete all data for person X" command exists.** Deletion requires removing records from each store individually (ChromaDB, CRM SQLite, search index). This is a known gap.

## Privacy Constraints for Developers

These rules apply to all code changes:

1. **Never log personal data content.** Log IDs, counts, and durations — not message bodies, email content, names, or contact details.
2. **Use synthetic data in documentation.** All examples use generic names, fake email addresses, and placeholder content.
3. **Claude API calls send minimal context.** Search results included in prompts should be the minimum needed to answer the query.
4. **No telemetry or analytics.** No usage tracking, no error reporting to external services, no crash dumps sent anywhere.
5. **No bulk data export APIs.** The API serves individual queries and CRUD operations, not data dumps.

```python
# GOOD — IDs and metadata only
logger.info(f"Indexed person {person_id}, {len(sources)} source entities")
logger.info(f"Search returned {len(results)} results in {duration_ms}ms")

# BAD — leaks personal data
logger.info(f"Indexed {person.display_name}: {person.email}, {person.phone}")
logger.info(f"Search result: {result.content[:200]}")
```

## macOS Security Integration

- **Full Disk Access (FDA)**: The `/Applications/LifeOS.app` wrapper has FDA permissions, allowing cron jobs to access `~/Documents/`. Only necessary operations (sync, indexing) route through this wrapper.
- **TCC (Transparency, Consent, and Control)**: The virtual environment is placed at `~/.venvs/lifeos` to avoid TCC scanning overhead (see [ADR-005](../../adr/005-external-venv-macos-tcc.md)).
- **FileVault**: Assumed to be enabled. LifeOS does not implement its own encryption at rest.

## Related Documents

**Design Context:**
- [ADR-005: External Venv](../../adr/005-external-venv-macos-tcc.md) — TCC scanning avoidance
- [Project Vision](../../vision/philosophy.md) — "Privacy Is the Foundation" principle

**Specifications:**
- [Architecture](architecture.md) — System architecture and deployment model
- [Data and Sync](data-and-sync.md) — What data is collected and how
- [Observability](observability.md) — What is logged and traced
- [Python Conventions](../standards/python-conventions.md) — Logging rules that enforce privacy

**Operational:**
- [AGENTS.md](../../../AGENTS.md) — Privacy principle and operational commands
