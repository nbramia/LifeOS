# Installation Guide

> **Status:** Complete
> **Last Updated:** 2026-07-09
> **Audience:** New users

> **Quick start**: If you have Claude Code, run it in the project root and point it at
> [setup.md](setup.md) -- it will walk you through the full setup interactively.

Complete walkthrough for setting up LifeOS on Linux or macOS.

---

## Start Here: Minimal vs Full Setup

LifeOS has two setup tiers. Do the minimal path first, confirm it works, then add
integrations as needed.

**Minimal setup** — a working hybrid-search corpus and chat at `/chat`:

1. An **Anthropic API key** (the default LLM backend calls Claude).
2. A **Markdown / Obsidian vault** to index (`LIFEOS_VAULT_PATH`).
3. **ChromaDB** running for vector search (`./scripts/chromadb.sh start`).

That's enough to index the vault, run semantic + keyword search, and chat over it.
CRM, relationship insights, and communication-gap features stay empty until a data
sync runs — they're populated by the optional integrations below, not by vault
indexing.

**Full setup** — layer these on when you want them (all optional, all addable later):

- **Google OAuth** — Gmail, Calendar, Drive sync
- **Slack** — workspace message sync
- **Monarch Money** — account balances, transactions, budgets
- **Telegram bot(s)** — chat interface and push notifications
- **Apple Data Agent** (macOS) — iMessage, phone calls, contacts
- **Local llama-server** — fully local LLM instead of the Anthropic backend (needs a high-VRAM GPU)
- **whisper-relay** — voice input
- **Agent worker** — autonomous execution of `#agent`-tagged tasks

The minimal path is Steps 1–8 below. The full-setup integrations are covered in
[Configuration](configuration.md), [Google OAuth](google-oauth.md),
[Slack Integration](slack-integration.md), and the interactive [Setup](setup.md) guide.

---

## Prerequisites

- **Linux** (primary) or **macOS** (required only for Apple Data Agent: iMessage, Contacts, Photos)
- **Python 3.11+**
- **Anthropic API key** — required for the default LLM backend (`LIFEOS_LLM_BACKEND=anthropic`), which handles orchestration and synthesis. Only optional if you run the fully-local path instead (`LIFEOS_LLM_BACKEND=local` + llama-server + a high-VRAM GPU — see [Local LLM (optional)](#local-llm-optional)).

---

## Step 1: Clone Repository

```bash
git clone https://github.com/yourusername/LifeOS.git
cd LifeOS
```

---

## Step 2: Create Virtual Environment

**Important**: Create the venv outside the project directory. This is a convention that also avoids macOS TCC security scanning delays if running on macOS.

```bash
# Create venv in ~/.venvs/ (recommended)
mkdir -p ~/.venvs
python3 -m venv ~/.venvs/lifeos

# Activate
source ~/.venvs/lifeos/bin/activate

# Install dependencies
pip install -r requirements.txt
```

**Why external venv?** Convention; keeps the project directory clean. On macOS, it also avoids TCC scanning delays when running via launchd.

---

## Step 3: Set Up ChromaDB

ChromaDB stores vector embeddings for semantic search. It runs as a separate server.

### Option A: Cron Watchdog (Recommended)

On Linux, ChromaDB can be managed via systemd (see `setup-systemd.sh`). On macOS, ChromaDB has issues with launchd (exit code 78), so use a cron watchdog instead:

```bash
# Add to crontab (crontab -e)
*/5 * * * * pgrep -f "chroma run" || (cd ~/LifeOS && ./scripts/chromadb.sh start)
```

### Option B: Manual Start

```bash
./scripts/chromadb.sh start
```

Verify ChromaDB is running:
```bash
curl http://localhost:8001/api/v2/heartbeat
```

---

## Step 4: Configure Environment

Copy the example environment file:

```bash
cp .env.example .env
```

A minimal `.env` to boot:

```bash
# Required
LIFEOS_VAULT_PATH=/path/to/your/obsidian/vault

# LLM backend: "anthropic" (default; requires ANTHROPIC_API_KEY) or "local" (requires llama-server on port 8080)
LIFEOS_LLM_BACKEND=anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Optional but recommended
LIFEOS_USER_NAME=YourFirstName
```

The full env-var reference (defaults, types, when-to-change notes for every `LIFEOS_*` and third-party variable) lives in [configuration.md](configuration.md).

> **Note:** `OLLAMA_*` variables are legacy and ignored — Ollama is no longer part of LifeOS. Do not install it.

---

## Step 5: Run Preflight Check

```bash
# Validate all prerequisites before starting
./scripts/server.sh preflight
```

Fix any failures before proceeding. Warnings are non-blocking.

---

## Step 6: Start Server

```bash
# Start the server (ALWAYS use this script, never run uvicorn directly)
./scripts/server.sh start

# Check status
./scripts/server.sh status
```

Web UI available at: http://localhost:8000

---

## Step 7: Verify Installation

Run the verification checklist:

```bash
# 1. Check server health
curl http://localhost:8000/health/full | jq

# 2. Check ChromaDB connection
curl http://localhost:8001/api/v2/heartbeat

# 3. Run tests
./scripts/test.sh
```

All checks should pass. If any fail, see [Troubleshooting](troubleshooting.md).

---

## Next Steps

1. **Configure integrations**: See [Configuration](configuration.md)
2. **Set up Google OAuth**: See [Google OAuth Guide](google-oauth.md)
3. **Set up systemd services** (Linux): `sudo ./scripts/setup-systemd.sh`
4. **Set up FDA wrapper** (macOS, for Apple Data Agent): `./scripts/create-lifeos-app.sh` — see [ADR-010: Apple Data Agent](../adr/010-apple-data-agent.md) for the design context (why a `.app` bundle, why rsync, what fails when)
5. **Configure launchd services** (macOS, legacy only — see [launchd-setup.md](launchd-setup.md) for why it's superseded post-Linux-migration)
6. **Run your first sync**: See [First Run Guide](first-run.md)

---

## Local LLM (optional)

The default backend is Anthropic (Claude via API). Running a fully-local LLM is an
**optional alternative** — set `LIFEOS_LLM_BACKEND=local` and run a llama-server
alongside LifeOS. It needs a high-VRAM GPU. LifeOS supports any GGUF model via
llama-server; a few are pre-configured:

| Model | VRAM | Notes |
|-------|------|-------|
| `unsloth/gemma-4-26B-A4B-it-GGUF` (default) | ~16 GB (Q4_K_M) | MoE — pairs well with the embedding model |
| `Qwen/Qwen3-32B-GGUF` | ~20 GB (Q4_K_M) | Strong general-purpose option |
| `ggml-org/gpt-oss-120b-GGUF` | ~59 GB (MXFP4) | Highest quality, but starves embeddings (sync stops the LLM automatically) |

### Switching models

```bash
# 1. Set the model in .env
LIFEOS_LLM_MODEL=Qwen/Qwen3-32B-GGUF

# 2. Reinstall systemd service (substitutes model into the service file)
sudo ./scripts/setup-systemd.sh

# 3. Restart (downloads the model on first start)
sudo systemctl restart lifeos-llm
```

The first start with a new model downloads the GGUF file. Subsequent starts use the cached file in `~/.cache/llama.cpp/`.

---

## GPU Memory (AMD Unified Memory Systems)

If running a local LLM on an AMD APU with unified memory (e.g., Ryzen AI MAX+), the BIOS allocates GPU vs CPU memory from a shared pool.

**Recommended allocation: 80 GB GPU** (for systems with 96+ GB total). This gives:
- ~59 GB for gpt-oss-120b (MXFP4) with 21 GB GPU headroom
- ~20 GB for Qwen3-32B (Q4_K_M) with 60 GB GPU headroom
- More RAM visible to the CPU than a larger GPU allocation would leave

The setup script creates an 8 GB swap file as an OOM safety net. The nightly sync pipeline automatically stops the LLM before embedding phases if GPU memory is insufficient, and restarts it afterward.

### Optional: cgroups for dev processes

To prevent test suites from triggering OOM kills:

```bash
# Create a memory-limited slice for dev work
sudo systemd-run --scope -p MemoryMax=16G --user bash
# Or add to ~/.config/systemd/user/dev.slice for persistence
```

## Common Issues

| Issue | Solution |
|-------|----------|
| ChromaDB won't start | Check port 8001 isn't in use: `lsof -i :8001` |
| Server won't start | Check port 8000: `./scripts/server.sh status` |
| Tests failing | Ensure ChromaDB is running |

See [Troubleshooting](troubleshooting.md) for detailed solutions.

## Related Documents

- [Configuration](configuration.md) -- Environment variables and config files
- [First Run](first-run.md) -- Post-installation first use guide
- [ADR-005: External Venv](../adr/005-external-venv-macos-tcc.md) -- Why the venv is outside the project
