# Installation Guide

> **Status:** Complete
> **Last Updated:** 2026-02-19
> **Audience:** New users

> **Quick start**: If you have Claude Code, run it in the project root and point it at
> [SETUP.md](setup.md) -- it will walk you through the full setup interactively.

Complete walkthrough for setting up LifeOS on Linux or macOS.

---

## Prerequisites

- **Linux** (primary) or **macOS** (required only for Apple Data Agent: iMessage, Contacts, Photos)
- **Python 3.11+**
- **Ollama** (for query routing; install via package manager or [ollama.com](https://ollama.com))
- **Anthropic API key** (optional — only needed if you prefer Claude over a local LLM; set `LIFEOS_LLM_BACKEND=anthropic` in `.env`)

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

## Step 3: Install Ollama

Ollama provides local LLM for query routing (determining if a query needs semantic search, keyword search, or both).

```bash
# Install Ollama
# Linux:
curl -fsSL https://ollama.com/install.sh | sh
# macOS:
brew install ollama

# Start Ollama service
ollama serve &

# Pull the routing model
ollama pull qwen2.5:7b-instruct
```

Verify Ollama is running:
```bash
curl http://localhost:11434/api/tags | jq
```

---

## Step 4: Set Up ChromaDB

ChromaDB stores vector embeddings for semantic search. It runs as a separate server.

### Option A: Cron Watchdog (Recommended)

On Linux, ChromaDB can be managed via systemd (see `setup-systemd.sh`). On macOS, ChromaDB has issues with launchd (exit code 78), so use a cron watchdog instead:

```bash
# Add to crontab (crontab -e)
*/5 * * * * pgrep -f "chroma run" || (cd /path/to/LifeOS && ./scripts/chromadb.sh start)
```

### Option B: Manual Start

```bash
./scripts/chromadb.sh start
```

Verify ChromaDB is running:
```bash
curl http://localhost:8001/api/v1/heartbeat
```

---

## Step 5: Configure Environment

Copy the example environment file:

```bash
cp .env.example .env
```

Edit `.env` with your settings:

```bash
# Required
LIFEOS_VAULT_PATH=/path/to/your/obsidian/vault

# LLM backend: "local" (default, uses llama-server on port 8080) or "anthropic"
LIFEOS_LLM_BACKEND=local

# Only required if LIFEOS_LLM_BACKEND=anthropic
# ANTHROPIC_API_KEY=sk-ant-...

# Optional but recommended
LIFEOS_USER_NAME=YourFirstName
```

See [Configuration Guide](CONFIGURATION.md) for all options.

---

## Step 6: Run Preflight Check

```bash
# Validate all prerequisites before starting
./scripts/server.sh preflight
```

Fix any failures before proceeding. Warnings are non-blocking.

---

## Step 7: Start Server

```bash
# Start the server (ALWAYS use this script, never run uvicorn directly)
./scripts/server.sh start

# Check status
./scripts/server.sh status
```

Web UI available at: http://localhost:8000

---

## Step 8: Verify Installation

Run the verification checklist:

```bash
# 1. Check server health
curl http://localhost:8000/health/full | jq

# 2. Check ChromaDB connection
curl http://localhost:8001/api/v1/heartbeat

# 3. Check Ollama
curl http://localhost:11434/api/tags | jq '.models[].name'

# 4. Run tests
./scripts/test.sh
```

All checks should pass. If any fail, see [Troubleshooting](../reference/TROUBLESHOOTING.md).

---

## Next Steps

1. **Configure integrations**: See [Configuration](CONFIGURATION.md)
2. **Set up Google OAuth**: See [Google OAuth Guide](../guides/GOOGLE-OAUTH.md)
3. **Set up systemd services** (Linux): `sudo ./scripts/setup-systemd.sh`
4. **Set up FDA wrapper** (macOS, for Apple Data Agent): `./scripts/create-lifeos-app.sh` — see [ADR-010: Apple Data Agent](../adr/010-apple-data-agent.md) for the design context (why a `.app` bundle, why rsync, what fails when)
5. **Configure launchd services** (macOS): See [Launchd Setup](../guides/LAUNCHD-SETUP.md)
6. **Run your first sync**: See [First Run Guide](FIRST-RUN.md)

---

## Local LLM Model Selection

LifeOS supports any GGUF model via llama-server. Two models are pre-configured:

| Model | VRAM | Quality | Embeddings coexist? |
|-------|------|---------|---------------------|
| `ggml-org/gpt-oss-120b-GGUF` (default) | ~59 GB | Highest | No — sync stops LLM automatically |
| `Qwen/Qwen3-32B-GGUF` | ~20 GB | Good | Yes — both fit in 80 GB GPU |

### Switching models

```bash
# 1. Set the model in .env
LIFEOS_LLM_MODEL=Qwen/Qwen3-32B-GGUF

# 2. Reinstall systemd service (substitutes model into the service file)
sudo ./scripts/setup-systemd.sh

# 3. Restart (downloads the model on first start)
sudo systemctl restart lifeos-llm
```

The first start with a new model downloads the GGUF file (~20 GB for Qwen3-32B Q4_K_M). Subsequent starts use the cached file in `~/.cache/llama.cpp/`.

---

## GPU Memory (AMD Unified Memory Systems)

If running a local LLM on an AMD APU with unified memory (e.g., Ryzen AI MAX+), the BIOS allocates GPU vs CPU memory from a shared pool.

**Recommended allocation: 80 GB GPU** (for systems with 96+ GB total). This gives:
- ~59 GB for gpt-oss-120b (MXFP4) with 21 GB GPU headroom
- ~20 GB for Qwen3-32B (Q4_K_M) with 60 GB GPU headroom
- ~46 GB visible to the CPU (vs ~30 GB at 96 GB GPU allocation)

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
| Ollama connection refused | Start Ollama: `ollama serve &` |
| Server won't start | Check port 8000: `./scripts/server.sh status` |
| Tests failing | Ensure ChromaDB and Ollama are running |

See [Troubleshooting](troubleshooting.md) for detailed solutions.

## Related Documents

- [Configuration](configuration.md) -- Environment variables and config files
- [First Run](first-run.md) -- Post-installation first use guide
- [ADR-005: External Venv](../adr/005-external-venv-macos-tcc.md) -- Why the venv is outside the project
