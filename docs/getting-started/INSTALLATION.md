# Installation Guide

Complete walkthrough for setting up LifeOS on macOS.

---

## Prerequisites

- **macOS** (required for Apple integrations: iMessage, Contacts, Photos)
- **Python 3.11+**
- **Homebrew** (for installing Ollama)
- **Anthropic API key** (for Claude synthesis)

---

## Step 1: Clone Repository

```bash
git clone https://github.com/yourusername/LifeOS.git
cd LifeOS
```

---

## Step 2: Create Virtual Environment

**Important**: Create the venv outside the project directory to avoid macOS TCC security scanning delays when running via launchd.

```bash
# Create venv in ~/.venvs/ (recommended for launchd)
mkdir -p ~/.venvs
python3 -m venv ~/.venvs/lifeos

# Activate
source ~/.venvs/lifeos/bin/activate

# Install dependencies
pip install -r requirements.txt
```

**Why external venv?** macOS Transparency, Consent, and Control (TCC) scans directories launched by launchd. Venvs inside the project directory can cause multi-minute delays on startup.

---

## Step 3: Install Ollama

Ollama provides local LLM for query routing (determining if a query needs semantic search, keyword search, or both).

```bash
# Install via Homebrew
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

ChromaDB has issues with launchd on macOS (exit code 78). Use a cron watchdog instead:

```bash
# Add to crontab (crontab -e)
* * * * * pgrep -f "chroma run" || (cd /path/to/LifeOS && ./scripts/chromadb.sh start)
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
ANTHROPIC_API_KEY=sk-ant-...
LIFEOS_VAULT_PATH=/path/to/your/obsidian/vault

# Optional but recommended
LIFEOS_USER_NAME=YourFirstName
```

See [Configuration Guide](CONFIGURATION.md) for all options.

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
3. **Configure launchd services**: See [Launchd Setup](../guides/LAUNCHD-SETUP.md)
4. **Run your first sync**: See [First Run Guide](FIRST-RUN.md)

---

## Common Issues

| Issue | Solution |
|-------|----------|
| ChromaDB won't start | Check port 8001 isn't in use: `lsof -i :8001` |
| Ollama connection refused | Start Ollama: `ollama serve &` |
| Server won't start | Check port 8000: `./scripts/server.sh status` |
| Tests failing | Ensure ChromaDB and Ollama are running |

See [Troubleshooting](../reference/TROUBLESHOOTING.md) for detailed solutions.
