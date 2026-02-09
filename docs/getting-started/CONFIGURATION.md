# Configuration Guide

Environment variables and configuration files for LifeOS.

---

## Environment Variables

### Required

| Variable | Description | Example |
|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Claude API key | `sk-ant-...` |
| `LIFEOS_VAULT_PATH` | Obsidian vault path | `/Users/you/Notes` |

### Server

| Variable | Description | Default |
|----------|-------------|---------|
| `LIFEOS_HOST` | Server bind address | `0.0.0.0` |
| `LIFEOS_PORT` | Server port | `8000` |
| `LIFEOS_CHROMA_URL` | ChromaDB server URL | `http://localhost:8001` |
| `LIFEOS_CHROMA_PATH` | ChromaDB data directory | `./data/chromadb` |

### User Identity

| Variable | Description | Example |
|----------|-------------|---------|
| `LIFEOS_USER_NAME` | Your first name (used in prompts) | `Nathan` |
| `LIFEOS_MY_PERSON_ID` | Your CRM person ID | UUID |
| `LIFEOS_WORK_DOMAIN` | Work email domain | `yourcompany.com` |

### Relationships

| Variable | Description | Example |
|----------|-------------|---------|
| `LIFEOS_PARTNER_NAME` | Partner's first name | `Taylor` |
| `LIFEOS_THERAPIST_PATTERNS` | Therapist names (pipe-separated) | `Dr. Smith\|Amy Morgan` |
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

### Google OAuth

| Variable | Description |
|----------|-------------|
| `GOOGLE_CREDENTIALS_PERSONAL` | Path to personal OAuth credentials |
| `GOOGLE_CREDENTIALS_WORK` | Path to work OAuth credentials |
| `GOOGLE_TOKEN_PERSONAL` | Path to personal OAuth token |
| `GOOGLE_TOKEN_WORK` | Path to work OAuth token |

### Slack

| Variable | Description | Example |
|----------|-------------|---------|
| `SLACK_USER_TOKEN` | User OAuth token | `xoxp-...` |
| `SLACK_TEAM_ID` | Workspace ID | `T02F5DW71LY` |

### Telegram

| Variable | Description | Example |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | `123456:ABC-DEF...` |
| `TELEGRAM_CHAT_ID` | Your chat ID (from `/getUpdates`) | `7145472553` |

When both are set, Telegram is enabled as a conversational client (full chat pipeline), scheduled reminder delivery channel, and alert destination. See [Reminders Guide](../guides/REMINDERS.md) for natural language reminder usage and [Claude Code Orchestration](../guides/CLAUDE-CODE-ORCHESTRATION.md) for running code tasks via `/code`.

### Claude Code Orchestration

| Variable | Description | Default |
|----------|-------------|---------|
| `LIFEOS_CLAUDE_BINARY` | Path to Claude CLI binary | `/Users/nathanramia/.local/bin/claude` |
| `LIFEOS_CLAUDE_TIMEOUT` | Max session runtime (seconds) | `600` |

Requires Claude Code installed and authenticated on the Mac Mini. See [Claude Code Orchestration Guide](../guides/CLAUDE-CODE-ORCHESTRATION.md#authentication-setup) for setup.

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
ANTHROPIC_API_KEY=sk-ant-your-key-here
LIFEOS_VAULT_PATH=/Users/you/Notes

# Identity
LIFEOS_USER_NAME=YourName
LIFEOS_WORK_DOMAIN=yourcompany.com

# Google OAuth
GOOGLE_CREDENTIALS_PERSONAL=./config/credentials-personal.json
GOOGLE_TOKEN_PERSONAL=./config/token-personal.json

# Slack
SLACK_USER_TOKEN=xoxp-your-token
SLACK_TEAM_ID=T02XXXXX

# Telegram (optional)
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_CHAT_ID=your-chat-id

# Notifications
LIFEOS_ALERT_EMAIL=you@email.com
```

---

## Next Steps

- [Google OAuth Setup](../guides/GOOGLE-OAUTH.md)
- [Slack Integration](../guides/SLACK-INTEGRATION.md)
- [First Run Guide](FIRST-RUN.md)
