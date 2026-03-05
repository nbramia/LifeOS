# Configuration Guide

> **Status:** Complete
> **Last Updated:** 2026-02-19
> **Audience:** Operators

Environment variables and configuration files for LifeOS.

---

## Environment Variables

### Required

| Variable | Description | Example |
|----------|-------------|---------|
| `LIFEOS_VAULT_PATH` | Obsidian vault path | `~/Notes` |

### Server

| Variable | Description | Default |
|----------|-------------|---------|
| `LIFEOS_HOST` | Server bind address | `0.0.0.0` |
| `LIFEOS_PORT` | Server port | `8000` |
| `LIFEOS_CHROMA_URL` | ChromaDB server URL | `http://localhost:8001` |
| `LIFEOS_CHROMA_PATH` | ChromaDB data directory | `./data/chromadb` |

### LLM Backend

| Variable | Description | Default |
|----------|-------------|---------|
| `LIFEOS_LLM_BACKEND` | LLM backend for chat synthesis: `local` (llama-server) or `anthropic` (Claude API) | `local` |
| `LIFEOS_LOCAL_LLM_URL` | Local LLM server URL | `http://localhost:8080` |
| `LIFEOS_LOCAL_LLM_TIMEOUT` | Local LLM request timeout (seconds) | `90` |
| `ANTHROPIC_API_KEY` | Claude API key (only required with `LIFEOS_LLM_BACKEND=anthropic`) | — |

### Embedding & Search

| Variable | Description | Default |
|----------|-------------|---------|
| `LIFEOS_EMBEDDING_MODEL` | Override embedding model | `mixedbread-ai/mxbai-embed-large-v1` |
| `LIFEOS_EMBEDDING_CACHE` | Embedding cache directory (empty = HuggingFace default) | — |
| `LIFEOS_RERANKER_MODEL` | Cross-encoder reranker model | `cross-encoder/ms-marco-MiniLM-L-6-v2` |
| `LIFEOS_RERANKER_ENABLED` | Enable/disable reranking | `true` |

### Ollama

| Variable | Description | Default |
|----------|-------------|---------|
| `OLLAMA_HOST` | Ollama server URL | `http://localhost:11434` |
| `OLLAMA_MODEL` | Model for query routing | `qwen2.5:7b-instruct` |
| `OLLAMA_TIMEOUT` | Request timeout (seconds) | `45` |
| `OLLAMA_RETRY_TIMEOUT` | Retry timeout (seconds) | `60` |

### User Identity

| Variable | Description | Example |
|----------|-------------|---------|
| `LIFEOS_USER_NAME` | Your first name (used in AI prompts) | `Alex` |
| `LIFEOS_MY_PERSON_ID` | Your CRM person ID (set after first sync) | UUID |
| `LIFEOS_WORK_DOMAIN` | Work email domain | `yourcompany.com` |
| `LIFEOS_TIMEZONE` | IANA timezone for schedules and prompts | `America/New_York` |

### Relationships

| Variable | Description | Example |
|----------|-------------|---------|
| `LIFEOS_PARTNER_NAME` | Partner's first name | `Taylor` |
| `LIFEOS_THERAPIST_PATTERNS` | Therapist names (pipe-separated) | `Dr. Smith\|Jane Doe` |
| `LIFEOS_PERSONAL_RELATIONSHIP_PATTERNS` | Personal meeting patterns | `Taylor\|Tay` |

### Vault Structure

| Variable | Description | Default |
|----------|-------------|---------|
| `LIFEOS_CURRENT_WORK_PATH` | Work folder prefix | `Work/` |
| `LIFEOS_PERSONAL_ARCHIVE_PATH` | Archive folder prefix | `Personal/zArchive/` |
| `LIFEOS_RELATIONSHIP_FOLDER` | Relationship folder name | `Relationship` |

### Colleagues

| Variable | Description | Example |
|----------|-------------|---------|
| `LIFEOS_CURRENT_COLLEAGUES` | Colleague first names (comma-separated) | `Alice,Bob,Charlie` |

### Multi-Account Sync

| Variable | Description | Default |
|----------|-------------|---------|
| `LIFEOS_WORK_DOMAIN_2` | Second work email domain | — |
| `LIFEOS_SYNC_WORK_GMAIL` | Enable work Gmail sync | `false` |
| `LIFEOS_SYNC_WORK_CALENDAR` | Enable work Calendar sync | `false` |
| `LIFEOS_SYNC_WORK2_GMAIL` | Enable 2nd work Gmail | `false` |
| `LIFEOS_SYNC_WORK2_CALENDAR` | Enable 2nd work Calendar | `false` |
| `LIFEOS_SYNC_SLACK` | Enable Slack sync | `false` |

All work sync toggles default to `false` for safety — work data is not indexed unless explicitly enabled.

### Google OAuth

Google OAuth credentials are stored at `config/credentials-{account}.json` and `config/token-{account}.json`. Use `scripts/google_auth.py` to authenticate.

### Slack

| Variable | Description | Example |
|----------|-------------|---------|
| `SLACK_CLIENT_ID` | Slack OAuth app client ID | `123456.789012` |
| `SLACK_CLIENT_SECRET` | Slack OAuth app client secret | `abc123...` |
| `SLACK_REDIRECT_URI` | Slack OAuth redirect URL | `http://localhost:8000/api/crm/slack/callback` |
| `SLACK_USER_TOKEN` | User OAuth token (direct auth, alternative to OAuth flow) | `xoxp-...` |
| `SLACK_TEAM_ID` | Workspace ID | `T02F5DW71LY` |

`SLACK_CLIENT_ID` and `SLACK_CLIENT_SECRET` are read by the settings module for OAuth flow. `SLACK_USER_TOKEN` and `SLACK_TEAM_ID` are read directly by the Slack sync service as an alternative to the OAuth flow.

### Telegram

| Variable | Description | Example |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | `123456:ABC-DEF...` |
| `TELEGRAM_CHAT_ID` | Your chat ID (from `/getUpdates`) | `7145472553` |

When both are set, Telegram is enabled as a conversational client (full chat pipeline), scheduled reminder delivery channel, and alert destination.

**Commands:**

| Command | Description |
|---------|-------------|
| `/new` | Start a new conversation (clears context) |
| `/status` | Check LifeOS server health |
| `/code <task>` | Run a task with Claude Code |
| `/code_status` | Check active Claude Code session |
| `/code_cancel` | Cancel active Claude Code session |
| `/help` | Show available commands |

**Natural language:** Send any message to query LifeOS, create tasks/reminders, or draft emails. See [Reminders Guide](../guides/REMINDERS.md) and [Task Management](../guides/TASK-MANAGEMENT.md) for examples. See [Claude Code Orchestration](../guides/CLAUDE-CODE-ORCHESTRATION.md) for `/code` details.

### Photos

| Variable | Description | Default |
|----------|-------------|---------|
| `LIFEOS_PHOTOS_PATH` | Apple Photos library path | `~/Pictures/Photos Library.photoslibrary` |

### Monarch Money

| Variable | Description | Default |
|----------|-------------|---------|
| `MONARCH_EMAIL` | Monarch Money email | — |
| `MONARCH_PASSWORD` | Monarch Money password | — |

### Claude Code Orchestration

| Variable | Description | Default |
|----------|-------------|---------|
| `LIFEOS_CLAUDE_BINARY` | Path to Claude CLI binary | `claude` (or full path) |
| `LIFEOS_CLAUDE_TIMEOUT` | Safety-net timeout (seconds) | `3600` |
| `LIFEOS_CLAUDE_MAX_TURNS` | Max Claude Code turns per session | `50` |
| `LIFEOS_CLAUDE_MAX_COST` | Max Claude Code cost per session (USD) | `2.0` |

Requires Claude Code installed and authenticated on the server. See [Claude Code Orchestration Guide](../guides/CLAUDE-CODE-ORCHESTRATION.md#authentication-setup) for setup.

### Backup

| Variable | Description | Default |
|----------|-------------|---------|
| `LIFEOS_BACKUP_PATH` | Backup directory | `./data/backups` |

### Notifications

| Variable | Description | Example |
|----------|-------------|---------|
| `LIFEOS_ALERT_EMAIL` | Email for sync failure alerts | `you@email.com` |

---

## Configuration Files

### People Dictionary

**File**: `config/people_dictionary.json` (gitignored)

Maps nicknames and aliases to canonical names:

```json
{
  "Al": "Alex",
  "Mike": "Michael",
  "Liz": "Elizabeth"
}
```

**Note**: Restart server after editing.

### Relationship Overrides

**File**: `config/relationship_overrides.json` (gitignored)

Force relationship strength/circle for specific people:

```json
{
  "strength_overrides": {
    "person-uuid": 100.0
  },
  "circle_overrides": {
    "person-uuid": 0
  }
}
```

### Family Members

**File**: `config/family_members.json` (gitignored)

List of family member person IDs for special handling.

---

## Data Directory

**Location**: `data/` (gitignored)

Contains:
- `crm.db` - SQLite database for people and interactions
- `chromadb/` - Vector embeddings
- `people_entities.json` - Canonical person records
- `imessage.db` - iMessage export cache

**Important**: This directory contains personal data. Back it up regularly but never commit it.

---

## ID Durability

When configuring overrides by person ID (strength, circle, tags), use **person IDs** not names:

- Names can change (renames, typos, merges)
- IDs are immutable UUIDs assigned at person creation

To find a person's ID:
```bash
curl "http://localhost:8000/api/crm/people?q=PersonName" | jq '.people[0].id'
```

---

## Example .env File

```bash
# Required
LIFEOS_VAULT_PATH=~/Notes

# LLM Backend (default: local)
# LIFEOS_LLM_BACKEND=local
# ANTHROPIC_API_KEY=sk-ant-your-key-here  # only needed with LIFEOS_LLM_BACKEND=anthropic

# Identity
LIFEOS_USER_NAME=YourName
LIFEOS_WORK_DOMAIN=yourcompany.com
LIFEOS_TIMEZONE=America/New_York

# Embedding & Search
# LIFEOS_EMBEDDING_MODEL=mixedbread-ai/mxbai-embed-large-v1
# LIFEOS_RERANKER_ENABLED=true

# Ollama
# OLLAMA_HOST=http://localhost:11434
# OLLAMA_MODEL=qwen2.5:7b-instruct

# Multi-Account Sync (all default to false)
# LIFEOS_SYNC_WORK_GMAIL=false
# LIFEOS_SYNC_WORK_CALENDAR=false
# LIFEOS_SYNC_SLACK=false

# Slack
SLACK_USER_TOKEN=xoxp-your-token
SLACK_TEAM_ID=T02XXXXX
# SLACK_CLIENT_ID=your-client-id
# SLACK_CLIENT_SECRET=your-client-secret

# Telegram (optional)
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_CHAT_ID=your-chat-id

# Monarch Money (optional)
# MONARCH_EMAIL=you@email.com
# MONARCH_PASSWORD=your-password

# Notifications
LIFEOS_ALERT_EMAIL=you@email.com
```

---

## Next Steps

- [Google OAuth Setup](google-oauth.md)
- [Slack Integration](slack-integration.md)
- [First Run Guide](first-run.md)

## Related Documents

- [Installation](installation.md) -- Initial installation walkthrough
- [First Run](first-run.md) -- Post-installation first use guide
