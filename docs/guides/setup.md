# LifeOS New Instance Setup

> **Status:** Complete
> **Last Updated:** 2026-08-09
> **Audience:** New users

Read this top-to-bottom. Execute each step, verify the check, then proceed.
Ask the user where marked **[ASK USER]**. Do not skip verification steps.

> **Minimal vs full setup:** The minimal path (Anthropic API key + a vault + ChromaDB)
> gives working hybrid search and chat; CRM/relationship features stay empty until an
> optional data sync runs. See
> [Installation → Start Here](installation.md#start-here-minimal-vs-full-setup) for the
> tier breakdown. This guide walks the full interactive setup; skip the optional
> integration phases you don't need.

---

## Phase 0: System Evaluation

Determine hardware capabilities to select appropriate models.

```bash
# Linux:
free -h
lscpu
uname -a

# macOS:
sysctl hw.memsize | awk '{print $2/1073741824 " GB RAM"}'
system_profiler SPHardwareDataType | grep "Chip\|Total Number of Cores\|Memory"
sw_vers
```

The orchestration/synthesis LLM runs on the **Anthropic backend by default** (Claude
via API — no local GPU needed). The embedding model runs locally regardless. Based on
available RAM, recommend an embedding + reranker configuration:

| RAM | Embedding Model | Reranker |
|-----|----------------|----------|
| 8 GB | `all-MiniLM-L6-v2` (384-dim) | disabled |
| 16 GB | `mixedbread-ai/mxbai-embed-large-v1` (1024-dim, default) | enabled |
| 32 GB+ | `mixedbread-ai/mxbai-embed-large-v1` (1024-dim, default) | enabled |
| 64 GB+ (GPU) | `Alibaba-NLP/gte-Qwen2-1.5B-instruct` (1536-dim, upgrade) | enabled |

The default embedding model is `mixedbread-ai/mxbai-embed-large-v1` (1024-dim).
`Alibaba-NLP/gte-Qwen2-1.5B-instruct` (1536-dim) is a recommended upgrade for capable
hardware. On 8 GB, drop to `all-MiniLM-L6-v2`.

A **fully-local LLM** (llama-server instead of the Anthropic backend) is an optional
alternative for 64 GB+ GPU systems — see [Installation → Local LLM](installation.md#local-llm-optional).

**[ASK USER]** Present the recommended configuration and confirm before proceeding.

Record the chosen models — they'll be set in Phase 3.

---

## Phase 1: Prerequisites

Check each prerequisite:

```bash
python3 --version   # Need 3.11+
git --version       # Need git

# Linux: ensure pip and venv are available
# Debian/Ubuntu: sudo apt install python3-pip python3-venv
# Fedora: sudo dnf install python3-pip

# macOS: Homebrew recommended
brew --version
```

**Note (macOS):** If Homebrew is not installed, the user must install it manually in a terminal
(it requires interactive `sudo`). Tell them to run:
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

If Python 3.11+ is missing:
```bash
# Linux (Debian/Ubuntu):
sudo apt install python3.12
# macOS:
brew install python@3.12
```

**[VERIFY]** Python is 3.11 or higher and git is available.

---

## Phase 2: Virtual Environment

Create the venv outside the project directory. This is a convention that also avoids macOS TCC scanning delays if running on macOS.

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
| `LIFEOS_VAULT_PATH` | Yes | Absolute path to Obsidian vault |
| `LIFEOS_USER_NAME` | Yes | First name (used in prompts) |
| `LIFEOS_LLM_BACKEND` | No | `anthropic` (default, uses Claude API) or `local` (uses llama-server — needs a high-VRAM GPU) |
| `ANTHROPIC_API_KEY` | Yes on default backend | Claude API key (starts with `sk-ant-`); required unless `LIFEOS_LLM_BACKEND=local` |
| `LIFEOS_PARTNER_NAME` | No | Partner's first name (leave empty to skip) |
| `LIFEOS_TIMEZONE` | No | IANA timezone (default: `America/New_York`) |

**[ASK USER]** Which optional integrations do you want to set up? (All can be added later.)

- **Google OAuth** — Calendar, Gmail, Drive sync (personal account)
- **Google OAuth (work)** — Separate work account for Calendar/Gmail (supports up to 2 work accounts)
- **Monarch Money** — Financial data (account balances, transactions, budgets)
- **Telegram bot** — Chat interface and push notifications
- **Slack** — Workspace message sync
- **WhatsApp** — Chat history sync via `wacli` on a paired Mac Mini (the LifeOS
  host runs on Linux; wacli is macOS-only). Data is exported into
  `data/apple-imports/whatsapp.json` and rsynced alongside contacts/iMessage.
- **MCP for Claude Code** — Use LifeOS as a tool from Claude Code/Desktop

Record the selections — they'll be configured in Phase 10.

If the user wants **Google OAuth (work)**, also collect:
- `LIFEOS_WORK_DOMAIN` — their work email domain (e.g., `acme.com`)
- Set `LIFEOS_SYNC_WORK_GMAIL=true` and/or `LIFEOS_SYNC_WORK_CALENDAR=true` as desired
- For a second work account: `LIFEOS_WORK_DOMAIN_2`, `LIFEOS_SYNC_WORK2_GMAIL`, `LIFEOS_SYNC_WORK2_CALENDAR`

If the user wants **Monarch Money**, collect:
- `MONARCH_EMAIL` — Monarch Money account email
- `MONARCH_PASSWORD` — Monarch Money account password

Set all collected values in `.env`. Then set model overrides from Phase 0:

- If 8 GB RAM:
  ```
  LIFEOS_EMBEDDING_MODEL=all-MiniLM-L6-v2
  LIFEOS_RERANKER_ENABLED=false
  ```
- If 16–32 GB RAM: defaults are fine, no overrides needed (embedding stays `mixedbread-ai/mxbai-embed-large-v1`).
- If 64 GB+ with GPU (upgrade):
  ```
  LIFEOS_EMBEDDING_MODEL=Alibaba-NLP/gte-Qwen2-1.5B-instruct
  ```

The default LLM backend is `anthropic`, so leave `LIFEOS_LLM_BACKEND` unset (or set it
to `anthropic`) and provide `ANTHROPIC_API_KEY`. `OLLAMA_*` variables are legacy and
ignored — do not set them.

**[VERIFY]** `.env` contains `LIFEOS_VAULT_PATH` with a real value (and `ANTHROPIC_API_KEY` unless using the `local` backend):

```bash
grep -E "^LIFEOS_VAULT_PATH=|^LIFEOS_LLM_BACKEND=|^ANTHROPIC_API_KEY=" .env
```

---

## Phase 4: Config Files

Copy example configs to their active names:

```bash
cp config/crm_mappings.yaml.example config/crm_mappings.yaml
cp config/linkedin_industry_mappings.json.example config/linkedin_industry_mappings.json
cp config/people_dictionary.example.json config/people_dictionary.json
cp config/family_members.example.json config/family_members.json
```

Then personalize the two people-related files:

**[ASK USER]** What is your first name? (Used to exclude self from entity extraction.)

Edit `config/people_dictionary.json` — replace the placeholder self entry with the
user's first name (as `canonical`) and lowercase aliases, keeping `"category": "self"`
and `"exclude": true`.

**[ASK USER]** What is your family last name? (Used for family member detection.)

Edit `config/family_members.json` — add the user's family last name (lowercase) to
`family_last_names`.

**[VERIFY]** All config files exist:

```bash
ls config/crm_mappings.yaml config/linkedin_industry_mappings.json config/people_dictionary.json config/family_members.json
```

---

## Phase 5: Services

### Start ChromaDB

```bash
./scripts/chromadb.sh start
```

Verify ChromaDB is healthy:

```bash
curl -s http://localhost:8001/api/v2/heartbeat
```

### Start the LifeOS server

```bash
./scripts/server.sh start
```

**Note:** LifeOS is designed to run its API server on exactly one machine; every
other machine should be a client pointed at it via `LIFEOS_API_URL`, not run its
own copy of the server. If you have multiple machines and want to enforce this,
set `LIFEOS_SERVER_HOSTNAME=<your-host>` (the output of `hostname` on the
designated machine) in `.env` — the server then refuses to start anywhere else.
Leave it unset for a single-machine setup; the default never blocks a start.

**[VERIFY]** All services are healthy:

```bash
curl -s http://localhost:8000/health/full | python3 -m json.tool
```

All services should show healthy status, with ChromaDB connected.

---

## Phase 6: First Index

Trigger the initial vault index. The embedding model will be downloaded automatically
on first run (~300MB for the default model, ~90MB for all-MiniLM-L6-v2). This is expected.

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

**Note:** Person entities are created during data syncs (contacts, email, calendar), not
during vault indexing. On a fresh install, the CRM will be empty after Phase 6.

Skip this phase for now. After running the first full sync (Phase 8 or manually via
`~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --execute`), come back and complete
this step:

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

## Phase 8: System Services (optional)

**[ASK USER]** Do you want to configure LifeOS to run automatically on boot?

### Linux (systemd)

```bash
sudo ./scripts/setup-systemd.sh
```

**[VERIFY]** systemd services are active:

```bash
systemctl status lifeos-api lifeos-chromadb
```

The systemd setup installs and enables the nightly `lifeos-sync.timer` only
after both `config/credentials-personal.json` and the authenticated
`config/token-personal.json` exist. This prevents a fresh VPS from running a
guaranteed failing sync job. After completing Google OAuth with
`scripts/google_auth.py --account personal`, re-run `setup-systemd.sh` to
activate Gmail/Calendar relationship history.

```bash
systemctl list-timers lifeos-sync.timer
~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --status
```

### macOS (launchd)

> **Note:** launchd is macOS-only and superseded by systemd on Linux — see
> [ADR-007: Linux Migration](../adr/007-linux-migration.md). Use this only on a macOS host.

`setup-launchd.sh` is interactive and takes no arguments — it prompts for the vault path:

```bash
./scripts/setup-launchd.sh
```

Set up the ChromaDB cron watchdog:

```bash
(crontab -l 2>/dev/null; echo "*/5 * * * * pgrep -f 'chroma run' || (cd $(pwd) && ./scripts/chromadb.sh start)") | crontab -
```

**[VERIFY]** launchd services are loaded:

```bash
launchctl list | grep lifeos
```

---

## Phase 9: FDA Wrapper (macOS Apple Data Agent only)

> **Note:** This phase applies only to macOS machines running the Apple Data Agent.
> On Linux, Apple data is imported via `apple_data_import.py` from nightly rsync exports.
> The design rationale (why a `.app` wrapper, why cron, why rsync, what fails when) is captured in [ADR-010: Apple Data Agent](../adr/010-apple-data-agent.md).

Phone calls, FaceTime, and iMessage sync **require Full Disk Access** to read system
databases. Without this phase, those data sources will not sync.

**[ASK USER]** Do you want to set up phone/iMessage sync?

If yes:

### Create the app bundle

```bash
./scripts/create-lifeos-app.sh --force
```

### Grant Full Disk Access

**[ASK USER]** The user must do these steps manually in System Settings:

1. Open **System Settings** → **Privacy & Security** → **Full Disk Access**
2. Click `+` and add `/Applications/LifeOS.app`
3. Also ensure **Terminal.app** has Full Disk Access (it's in `/Applications/Utilities/`)

### Add the cron job for nightly FDA sync

```bash
(crontab -l 2>/dev/null; echo "50 2 * * * /Applications/LifeOS.app/Contents/MacOS/LifeOS fda-sync") | crontab -
```

This runs at 2:50 AM, 10 minutes before the main nightly sync.

**[VERIFY]** The app bundle exists and is executable:

```bash
/Applications/LifeOS.app/Contents/MacOS/LifeOS watchdog
```

---

## Phase 10: Optional Integrations

Set up the integrations selected in Phase 3. Skip any that weren't selected.

### Google OAuth — Personal (Calendar, Gmail, Drive)

Follow [google-oauth.md](google-oauth.md). Key steps:
1. Create Google Cloud project and enable Calendar/Gmail/Drive APIs
2. Configure OAuth consent screen and **publish the app** (Audience → Publish)
3. Create OAuth credentials, save as `config/credentials-personal.json`
4. Run: `~/.venvs/lifeos/bin/python scripts/google_auth.py --account personal`

### Google OAuth — Work (Calendar, Gmail)

If the user selected work Google account in Phase 3:
1. Create a **separate** Google Cloud project for the work account
2. Follow the same OAuth setup steps as personal
3. Save credentials as `config/credentials-work.json`
4. Run: `~/.venvs/lifeos/bin/python scripts/google_auth.py --account work`
5. Verify `LIFEOS_WORK_DOMAIN`, `LIFEOS_SYNC_WORK_GMAIL`, and/or `LIFEOS_SYNC_WORK_CALENDAR`
   are set in `.env` (should already be from Phase 3)

For a **second work account**, repeat with `--account work2`:
1. Save credentials as `config/credentials-work2.json`
2. Run: `~/.venvs/lifeos/bin/python scripts/google_auth.py --account work2`
3. Set `LIFEOS_WORK_DOMAIN_2`, `LIFEOS_SYNC_WORK2_GMAIL`, `LIFEOS_SYNC_WORK2_CALENDAR` in `.env`

### Telegram Bot

1. Create a bot via @BotFather on Telegram
2. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to `.env`
3. Restart: `./scripts/server.sh restart`

### Slack

1. Set up workspace OAuth
2. Add `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_USER_TOKEN` to `.env`
3. Restart: `./scripts/server.sh restart`

### WhatsApp

WhatsApp sync runs on the Mac Mini side of the Apple Data Agent pipeline —
the Linux LifeOS host does not touch wacli directly. These steps must be run
on the Mac Mini that rsyncs `data/apple-imports/` to the Linux server.

On the Mac Mini:

1. Install: `brew install steipete/tap/wacli`
2. Authenticate: `wacli auth` (scan the QR code with WhatsApp → Linked Devices)
3. Verify: `wacli chats list --limit 5`
4. Confirm the exporter picks up WhatsApp:
   `~/.venvs/lifeos/bin/python scripts/apple_data_export.py --execute --source whatsapp`
   should produce `data/apple-imports/whatsapp.json`.

On the Linux host, no configuration is needed — the `apple_import` sync step
already imports `whatsapp.json` through `scripts/apple_data_import.py`. If the
Mac side can't run wacli (e.g. not installed, not authenticated), the manifest
marks WhatsApp as `status: "error"`, the Linux importer logs CRITICAL, and
`sync_health` records the apple_import run as FAILED so you get alerted.

### MCP for Claude Code

**Note:** This command must be run in a separate terminal, not from within Claude Code.
Point it at your LifeOS checkout (uses stdio transport):

```bash
claude mcp add lifeos -- ~/.venvs/lifeos/bin/python ~/LifeOS/mcp_server.py
```

### Monarch Money (Financial Data)

`MONARCH_EMAIL` and `MONARCH_PASSWORD` should already be set in `.env` from Phase 3.
Now run the interactive MFA authentication (a code will be sent via email/SMS):

**[ASK USER]** The user needs to be ready to enter an MFA code from their email/phone.

```bash
~/.venvs/lifeos/bin/python -c "
import asyncio
from monarchmoney import MonarchMoney
mm = MonarchMoney()
asyncio.run(mm.interactive_login())
mm.save_session('data/monarch_session.pickle')
print('Session saved!')
"
```

Restart: `./scripts/server.sh restart`

Each integration can be added later. None are required for core functionality.

---

## Done

LifeOS is running. Core functionality available:

- Semantic search: `POST /api/search`
- Chat: `POST /api/chat`
- CRM: `GET /api/crm/people`
- Health: `GET /health/full`

**Remember:** Come back to Phase 7 after the first full sync to set your person ID.

For ongoing maintenance, see the project `CLAUDE.md` and `README.md`.

## Related Documents

- [Installation](installation.md) -- Manual installation walkthrough
- [Configuration](configuration.md) -- Environment variables and config files
- [First Run](first-run.md) -- Post-installation verification
