# LifeOS New Instance Setup

Read this top-to-bottom. Execute each step, verify the check, then proceed.
Ask the user where marked **[ASK USER]**. Do not skip verification steps.

---

## Phase 0: System Evaluation

Determine hardware capabilities to select appropriate models.

```bash
sysctl hw.memsize | awk '{print $2/1073741824 " GB RAM"}'
system_profiler SPHardwareDataType | grep "Chip\|Total Number of Cores\|Memory"
sw_vers
```

Based on available RAM, recommend a model configuration:

| RAM | Embedding Model | Ollama Model | Reranker |
|-----|----------------|--------------|----------|
| 8 GB | `all-MiniLM-L6-v2` | `qwen2.5:1.5b-instruct` | disabled |
| 16 GB | `mixedbread-ai/mxbai-embed-large-v1` | `qwen2.5:3b-instruct` | enabled |
| 32 GB+ | `mixedbread-ai/mxbai-embed-large-v1` | `qwen2.5:7b-instruct` | enabled |

**[ASK USER]** Present the recommended configuration and confirm before proceeding.

Record the chosen models — they'll be set in Phase 3.

---

## Phase 1: Prerequisites

Check each prerequisite:

```bash
python3 --version   # Need 3.11+
brew --version      # Need Homebrew
git --version       # Need git
```

Install anything missing:

```bash
# If Homebrew is missing:
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# If Python 3.11+ is missing:
brew install python@3.12
```

**[VERIFY]** All three commands return valid versions. Python is 3.11 or higher.

---

## Phase 2: Virtual Environment

Create the venv outside the project to avoid macOS TCC scanning delays with launchd.

```bash
mkdir -p ~/.venvs
python3 -m venv ~/.venvs/lifeos
~/.venvs/lifeos/bin/pip install --upgrade pip
~/.venvs/lifeos/bin/pip install -r requirements.txt
```

**[VERIFY]** Confirm key packages are installed:

```bash
~/.venvs/lifeos/bin/pip show sentence-transformers fastapi chromadb | grep -E "^Name:|^Version:"
```

All three packages should be listed.

---

## Phase 3: Environment Configuration

```bash
cp .env.example .env
```

**[ASK USER]** Collect the following values:

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Claude API key (starts with `sk-ant-`) |
| `LIFEOS_VAULT_PATH` | Yes | Absolute path to Obsidian vault |
| `LIFEOS_USER_NAME` | Yes | First name (used in prompts) |
| `LIFEOS_PARTNER_NAME` | No | Partner's first name (leave empty to skip) |
| `LIFEOS_TIMEZONE` | No | IANA timezone (default: `America/New_York`) |

Set these values in `.env`. Then set model overrides from Phase 0:

- If 8 GB RAM:
  ```
  LIFEOS_EMBEDDING_MODEL=all-MiniLM-L6-v2
  OLLAMA_MODEL=qwen2.5:1.5b-instruct
  LIFEOS_RERANKER_ENABLED=false
  ```
- If 16 GB RAM:
  ```
  OLLAMA_MODEL=qwen2.5:3b-instruct
  ```
- If 32 GB+ RAM: defaults are fine, no overrides needed.

**[VERIFY]** `.env` contains `ANTHROPIC_API_KEY` and `LIFEOS_VAULT_PATH` with real values:

```bash
grep -E "^ANTHROPIC_API_KEY=|^LIFEOS_VAULT_PATH=" .env
```

---

## Phase 4: Config Files

Copy example configs to their active names:

```bash
cp config/crm_mappings.yaml.example config/crm_mappings.yaml
cp config/linkedin_industry_mappings.json.example config/linkedin_industry_mappings.json
```

Create `config/people_dictionary.json` with the user's self entry:

**[ASK USER]** What is your first name? (Used to exclude self from entity extraction.)

```json
{
  "FIRST_NAME": {
    "canonical": "FIRST_NAME",
    "aliases": ["FIRST_NAME_LOWER", "me", "I"],
    "category": "self",
    "exclude": true
  }
}
```

Replace `FIRST_NAME` and `FIRST_NAME_LOWER` with the user's name.

Create `config/family_members.json`:

**[ASK USER]** What is your family last name? (Used for family member detection.)

```json
{
  "family_last_names": ["LAST_NAME_LOWER"],
  "family_exact_names": []
}
```

Replace `LAST_NAME_LOWER` with the user's family last name in lowercase.

**[VERIFY]** All config files exist:

```bash
ls config/crm_mappings.yaml config/linkedin_industry_mappings.json config/people_dictionary.json config/family_members.json
```

---

## Phase 5: Services

### Install and start Ollama

```bash
brew install ollama
brew services start ollama
```

Wait a few seconds for Ollama to start, then pull the selected model:

```bash
ollama pull MODEL_NAME_FROM_PHASE_0
```

### Start ChromaDB

```bash
./scripts/chromadb.sh start
```

### Start the LifeOS server

```bash
./scripts/server.sh start
```

**[VERIFY]** All services are healthy:

```bash
curl -s http://localhost:8000/health/full | python3 -m json.tool
```

All services should show healthy status. ChromaDB and Ollama should be connected.

---

## Phase 6: First Index

Trigger the initial vault index:

```bash
curl -s -X POST http://localhost:8000/api/admin/reindex/sync | python3 -m json.tool
```

This may take several minutes depending on vault size. Wait for it to complete.

Then test search:

```bash
curl -s -X POST http://localhost:8000/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "test", "top_k": 5}' | python3 -m json.tool
```

**[VERIFY]** Search returns results from the vault. If the vault has content, `results` should be non-empty.

---

## Phase 7: Person ID Setup

Look up the user's person entity ID:

```bash
curl -s "http://localhost:8000/api/crm/people?q=FIRST_NAME" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for p in data.get('people', []):
    print(f\"{p['id']}  {p['name']}\")
"
```

**[ASK USER]** Confirm which entry is them.

Add their person ID to `.env`:

```
LIFEOS_MY_PERSON_ID=the-id-from-above
```

Restart the server:

```bash
./scripts/server.sh restart
```

**[VERIFY]** Server is healthy after restart:

```bash
curl -s http://localhost:8000/health/full | python3 -m json.tool
```

---

## Phase 8: Launchd Services (optional)

**[ASK USER]** Do you want to configure LifeOS to run automatically on boot?

If yes:

```bash
./scripts/setup-launchd.sh
```

Set up the ChromaDB cron watchdog:

```bash
(crontab -l 2>/dev/null; echo "*/5 * * * * pgrep -f 'chroma run' || (cd $(pwd) && ./scripts/chromadb.sh start)") | crontab -
```

**[VERIFY]** Launchd services are loaded:

```bash
launchctl list | grep lifeos
```

---

## Phase 9: Optional Integrations

**[ASK USER]** Which integrations do you want to set up?

- **Google OAuth** (Calendar, Gmail, Drive) — follow `docs/guides/GOOGLE-OAUTH.md`
- **Telegram bot** — create a bot via @BotFather, add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to `.env`
- **Slack** — set up workspace OAuth, add `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_USER_TOKEN` to `.env`
- **MCP for Claude Code** — `claude mcp add lifeos -- curl -s -X POST http://localhost:8000/api/mcp`
- **LifeOS.app FDA wrapper** (for iMessage/Phone sync) — `./scripts/create-lifeos-app.sh`

Each integration can be added later. None are required for core functionality.

---

## Done

LifeOS is running. Core functionality available:

- Semantic search: `POST /api/search`
- Chat: `POST /api/chat`
- CRM: `GET /api/crm/people`
- Health: `GET /health/full`

For ongoing maintenance, see the project `CLAUDE.md` and `README.md`.
