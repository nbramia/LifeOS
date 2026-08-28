# Installation Guide

> **Status:** Complete
> **Last Updated:** 2026-08-28
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

## Running LifeOS on macOS as the Host

The minimal setup (Steps 1–7 below) works identically on macOS and Linux —
Anthropic API key, vault, ChromaDB, working `/chat`. Past that point, most of
the packaged "keep itself healthy unattended" automation is Linux-only:
**17 systemd units** ship for Linux (sync, three crash-restart watchdogs,
autodeploy, agent worker, MCP-HTTP, local LLM) versus **3 launchd templates**
for macOS (`com.lifeos.api`, `com.lifeos.crm-sync`, and an unused
`com.lifeos.chromadb` — ChromaDB uses a cron watchdog instead, see Step 3).
See [launchd-setup.md](launchd-setup.md) for why macOS-as-primary-server was
superseded by the Linux migration and what's preserved from it.

**Nightly sync does have a packaged macOS path.** `./scripts/setup-launchd.sh`
(Phase 8 of [setup.md](setup.md)) installs `com.lifeos.crm-sync`, a launchd
agent that runs `scripts/run_sync_wrapper.sh --execute --trigger=scheduled`
at 3 AM — a wrapper that adds preflight checks, `LIFEOS_HEADLESS=true`, and a
6-hour runtime watchdog around the bare sync script. It does not restart
itself on failure (`KeepAlive: false` — a crashed run just waits for the
next scheduled tick). If you'd rather skip launchd entirely, a plain cron
entry calling `run_all_syncs.py` directly is simpler but **not equivalent**
— it skips the wrapper's preflight checks, headless-mode env var, and
watchdog, so a hang just runs until cron's own environment kills it (if
ever):

```bash
# Add to crontab (crontab -e) -- runs the full nightly sync at 3:00 AM
# Replace /path/to/LifeOS with wherever you cloned the repo.
0 3 * * * cd /path/to/LifeOS && ~/.venvs/lifeos/bin/python scripts/run_all_syncs.py --execute --trigger scheduled >> logs/sync.log 2>&1
```

**What you're living without, either way:** if a sync run hangs or crashes,
nothing restarts it until the next scheduled tick (no `lifeos-watchdog`
equivalent); pushing to `main` doesn't auto-redeploy the running server (no
`lifeos-autodeploy`); and the agent worker and MCP-HTTP bridge aren't
packaged for launchd at all. None of that blocks chat, search, or manual
sync runs — it's the unattended-operations layer, and it's Linux-only today.

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

# LLM backend: "anthropic" (default; requires ANTHROPIC_API_KEY), "local" (requires
# llama-server on port 8080), or "remote" (any OpenAI-compatible hosted provider, e.g.
# Fireworks — requires LIFEOS_REMOTE_LLM_URL/_MODEL/_API_KEY, see configuration.md)
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

**On a 16 GB machine (e.g. a base Mac mini), the default local model has zero
headroom** — 16 GB of VRAM/unified memory for a ~16 GB model leaves nothing
for the OS, the embedding model, or the server process itself. Don't try to
force the local path on a small machine. Anthropic-API-only
(`LIFEOS_LLM_BACKEND=anthropic`, the default — see [Step 4](#step-4-configure-environment))
is the intended mode for small machines and is fully functional: it's what
the entire Minimal Setup tier above already runs on. Treat "Local LLM" as
something you opt into on hardware that has the VRAM for it, not a
requirement.

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

## Setting up for a second user (config-only)

A common deployment: someone other than the maintainer clones LifeOS onto
their own machine (e.g. a family member's Mac mini), talks to it through an
external Hermes front door instead of `/chat` directly, and never touches
the repo's own source files. This checklist is that path.

1. **Minimal tier, as above.** Vault path + Anthropic key + ChromaDB gets you
   a working `/chat` (Steps 1–7). Every person-specific value lives in files
   git doesn't track: `.env` (copied from `.env.example`) and, if you use
   them, `config/*.json` / `config/*.yaml` files copied from their
   `.example`/`.example.yaml` templates (e.g.
   `config/people_dictionary.example.json`,
   `config/family_members.example.json` — full list in
   [Configuration § Configuration Files](configuration.md#configuration-files)). You should never need
   to edit a tracked file in the repo itself to configure an instance.
   After the first sync, `scripts/setup_identity.py` turns picking yourself,
   your partner, and your family out of the indexed people into a guided
   conversation instead of hand-editing those files — see
   [First Run § After First Sync](first-run.md#after-first-sync-set-your-identity).
2. **Connect an external Hermes.** Add `LIFEOS_HERMES_BACKEND_URL` (and
   `LIFEOS_HERMES_BACKEND_TOKEN` if Hermes requires one) directly to your
   `.env` — both are deliberately absent from `.env.example` since they're
   opt-in. Once set, `/chat` shows a **LifeOS | Agent | Hermes** backend
   selector and defaults new sessions to Hermes automatically. **The
   fallback to plain LifeOS fires whenever Hermes isn't actually usable** —
   `GET /api/hermes/status` probes reachability (cached, short-TTL), not
   just whether `LIFEOS_HERMES_BACKEND_URL` is set, so a configured-but-down
   Hermes defaults `/chat` to plain LifeOS instead of failing every turn.
   The Hermes option stays visible but marked unavailable rather than
   silently vanishing, and picks back up automatically once Hermes is
   reachable again — no restart needed. See
   [client-surfaces.md](../specs/technical/client-surfaces.md) for the exact
   contract and [voice-setup.md § Optional Agent and Hermes text
   backends](voice-setup.md#optional-agent-and-hermes-text-backends) for the
   full variable reference. **Standing up the Hermes adapter service itself
   is Hermes-repo setup — out of scope here.** LifeOS only proxies to a URL
   you already have running.
3. **Skip the local LLM.** API-only mode (`LIFEOS_LLM_BACKEND=anthropic`,
   the default) is the intended small-machine path — see the 16 GB caveat in
   [Local LLM (optional)](#local-llm-optional) above. Leave
   `LIFEOS_LLM_MODEL*` unset entirely.
4. **Leave everything else unconfigured.** Google OAuth (beyond whatever
   account you actually want indexed), Slack, Monarch Money, a dedicated
   Telegram bot, the Apple Data Agent — any of these you don't set up simply
   stay unset. The nightly sync treats an unconfigured source as a clean
   skip, not a failure: Apple-only sources report `skipped` on a
   Linux/no-Mac host, and Monarch Money, the personal Google account, and
   the gsheet-journal source report "skipped (not configured)" the same way
   — see [data-and-sync.md](../specs/technical/data-and-sync.md). Either
   way, the rest of the sync still succeeds.
5. **macOS as the host.** If this second machine is a Mac, see
   [Running LifeOS on macOS as the Host](#running-lifeos-on-macos-as-the-host)
   above for what's packaged (nightly sync) versus what you'll want to
   hand-roll (crash-restart watchdogs, autodeploy).

## Related Documents

- [Configuration](configuration.md) -- Environment variables and config files
- [First Run](first-run.md) -- Post-installation first use guide
- [Launchd Setup](launchd-setup.md) -- macOS service automation: what's packaged (`com.lifeos.api`, `com.lifeos.crm-sync`), what's superseded, and why
- [Client Surfaces](../specs/technical/client-surfaces.md) -- The Agent/Hermes backend contract and fallback behavior referenced above
- [ADR-005: External Venv](../adr/005-external-venv-macos-tcc.md) -- Why the venv is outside the project
