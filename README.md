# LifeOS

**Your personal knowledge graph, built from the digital exhaust of your life.**

LifeOS is a self-hosted AI assistant that connects to your Gmail, Google Calendar, iMessage, WhatsApp, Slack, Obsidian vault, Granola meeting transcriptions, Google Docs, iPhotos, LinkedIn, and Apple contacts — then makes all of it **searchable and queryable through natural language.** 
- Ask questions of your unified data through Telegram or a dedicated chat UI 
- Visualize and explore your relationships with each person in your life through a CRM UI
- Manage tasks, reminders and alerts (pushed to you through email and Telegram) in natural language
- Or enable Claude Code to interface with the full range of MCP tools directly

The people we are closest to appear across multiple channels. Your colleague is `katie.smith@company.com` in Gmail and calendar invites, `Kate Smith` in email bodies and your personal notes, `+1-901-555-1234` in iMessage and WhatsApp, and `@ksmith` on Slack - and iPhoto has tied her face to her contact in your phone. LifeOS automatically resolves these fragments into unified person records, surfacing this unified information and making it immediately accessible.

Everything runs locally on your Mac. Your data never leaves your machine—only query synthesis calls the Claude API. A nightly sync pulls from your data sources, indexes everything for hybrid search (semantic + keyword), and keeps your knowledge graph fresh.

---

## What You Can Do

- **Ask questions about your life**: "When did I last talk to Mom?" / "What's the context for my meeting with Acme Corp tomorrow?" and get quick answers and briefs
- **Search across everything**: Ask "What were the key recommendations Sarah made on the Acme project last month?" and get an answer synthesized from hybrid semantic + keyword search across notes, emails, messages, calendar, and more
- **Track relationships**: Ask "Who am I engaging with less than I used to? Who should I reconnect with?" and see interaction history, communication patterns, and relationship strength over time
- **Surface old facts and ideas**: Ask "What should I get Jane for her birthday" and it'll pull context from calendar events, email threads, and text messages up to 10 years old
- **Manage tasks naturally**: "Remind me to follow up with John next Tuesday" creates an Obsidian task or a push reminder
- **Prepare for meetings**: Get briefings with attendee history, past discussions, and relevant notes
- **Use from Claude Code**: MCP tools let AI assistants query your personal knowledge

---

## Quick Links

| Getting Started | Guides | Reference |
|-----------------|--------|-----------|
| [Installation](docs/getting-started/INSTALLATION.md) | [Google OAuth](docs/guides/GOOGLE-OAUTH.md) | [API Reference](docs/architecture/API-MCP-REFERENCE.md) |
| [Configuration](docs/getting-started/CONFIGURATION.md) | [Slack Integration](docs/guides/SLACK-INTEGRATION.md) | [Scripts](docs/reference/SCRIPTS.md) |
| [First Run](docs/getting-started/FIRST-RUN.md) | [Task Management](docs/guides/TASK-MANAGEMENT.md) | [Troubleshooting](docs/reference/TROUBLESHOOTING.md) |
|  | [Reminders](docs/guides/REMINDERS.md) | |
|  | [Launchd Setup](docs/guides/LAUNCHD-SETUP.md) | |

---

## Requirements

- **macOS** (required for Apple integrations)
- **Python 3.11+**
- **Anthropic API key**
- Obsidian vault (or other markdown notes)

---

## Quick Start

```bash
# 1. Clone and setup
git clone https://github.com/yourusername/LifeOS.git
cd LifeOS
python3 -m venv ~/.venvs/lifeos
source ~/.venvs/lifeos/bin/activate
pip install -r requirements.txt

# 2. Install Ollama
brew install ollama && ollama serve &
ollama pull qwen2.5:7b-instruct

# 3. Configure
cp .env.example .env
# Edit .env with your settings

# 4. Start services
./scripts/chromadb.sh start
./scripts/server.sh start

# 5. Open http://localhost:8000
```

See [Installation Guide](docs/getting-started/INSTALLATION.md) for detailed instructions.

---

## Architecture

![LifeOS Architecture](docs/images/architecture-hero.png)

### Search Pipeline

Different query types are handled by different pipelines:

```mermaid
flowchart LR
    Q["User Query"] --> Router["Router\n(local Ollama)"]

    Router -->|"General"| Direct["Direct Answer\n(Anthropic API)"]
    Router -->|"Web"| Web["Web Search\n(Anthropic API)"]
    Router -->|"Personal"| Hybrid["Hybrid Search\n(local)"]
    Router -->|"Compound"| Both["Web + Personal"]

    Hybrid --> Syn["Synthesis\n(Anthropic API)"]
    Both --> Syn
    Direct --> Response["Response"]
    Web --> Response
    Syn --> Response
```

**Query types:**
- **General knowledge**: "What's the capital of France?" → Claude answers directly
- **Web search**: "What's the weather in NYC?" → Uses web_search tool
- **Personal data**: "What did I discuss with John last week?" → Searches your data
- **Compound**: "Look up the trash schedule and remind me the night before" → Multiple actions

### CRM UI

Translates 10 years of interaction history with thousands of contacts into insights and visualizations.

<details>
<summary><strong>Pages aggregating contact details and interaction history for each person you know.</strong></summary>

![Person page](docs/images/person.png)

</details>

<details>
<summary><strong>Visualize how your communication patterns have evolved over the last 10 years.</strong></summary>

![Dashboard page](docs/images/dashboard.png)

</details>

<details>
<summary><strong>Dive deeper on relationships with your family and partner.</strong></summary>

![Dashboard page](docs/images/family.png)

</details>

<details>
<summary><strong>Visualize and explore relationships in a dynamic social graph.</strong></summary>

![Close graph page](docs/images/close_graph.png)

![Far graph page](docs/images/far_graph.png)

</details>

---

## Data Sources

| Source | Method | Data |
|--------|--------|------|
| Obsidian | File watcher | Notes, mentions |
| Gmail | Google API | Emails, threads |
| Calendar | Google API | Events, attendees |
| iMessage | macOS chat.db | Messages |
| Slack | Slack API | DMs, users |
| Contacts | Apple CSV | Names, emails, phones |
| Photos | Photos.sqlite | Face recognition |
| LinkedIn | CSV import | Connections |

<details>
<summary><strong>Sync Phases (Daily 3AM)</strong></summary>

The unified daily sync runs in 5 phases with dependencies:

```mermaid
flowchart LR
    subgraph P1["1: Collection"]
        direction TB
        G1[Gmail]
        C1[Calendar]
        IM[iMessage]
        Sl[Slack]
    end

    subgraph P2["2: Entity"]
        direction TB
        Link["Link sources\nto people"]
    end

    subgraph P3["3: Relationships"]
        direction TB
        Rel["Discover &\ncalculate strength"]
    end

    subgraph P4["4: Indexing"]
        direction TB
        Idx["ChromaDB +\nBM25 reindex"]
    end

    subgraph P5["5: Content"]
        direction TB
        Con["Google Docs\n& Sheets"]
    end

    P1 --> P2 --> P3 --> P4 --> P5
```

**Why:**
1. Data Collection must complete before Entity Processing can link records
2. Entity Processing must complete before Relationship Building has linked entities
3. Relationship Building must complete before Vector Indexing has fresh CRM data
4. Content Sync runs last (indexed on next cycle)

</details>

<details>
<summary><strong>Service Dependencies</strong></summary>

Services are categorized by criticality and fallback behavior:

```mermaid
flowchart LR
    subgraph Local["Local (Critical)"]
        direction TB
        ChromaDB["ChromaDB\n:8001"]
        Embed["Embedding\nModel"]
        Vault["Vault\nFilesystem"]
    end

    subgraph Fallback["Local (With Fallback)"]
        direction TB
        Ollama["Ollama\n:11434"] -->|fallback| Haiku["Anthropic\nHaiku"]
        BM25["BM25"] -->|fallback| VecOnly["Vector-only"]
    end

    subgraph External["External APIs"]
        direction TB
        GCal["Google\nCalendar"]
        Gmail["Google\nGmail"]
        Anthropic["Anthropic\nClaude"]
    end

    style Local fill:#ffcccc
    style Fallback fill:#fff3cd
    style External fill:#d4edda
```

**Severity levels:**
- **CRITICAL**: Sent immediately (ChromaDB down, embedding failed, vault inaccessible)
- **WARNING**: Batched nightly (Ollama unavailable, backup failed)
- **INFO**: Log only (Telegram retry, config defaults used)

</details>

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Embeddings | sentence-transformers |
| Vector DB | ChromaDB |
| Keyword Search | SQLite FTS5 |
| Query Router | Ollama + Qwen 2.5 |
| Synthesis | Claude API |
| Backend | FastAPI |
| Frontend | Vanilla JS |

---

## Documentation

### Architecture
- [Data & Sync](docs/architecture/DATA-AND-SYNC.md) - Data sources and sync processes
- [API & MCP Reference](docs/architecture/API-MCP-REFERENCE.md) - API endpoints and MCP tools
- [Frontend](docs/architecture/FRONTEND.md) - UI components

### PRDs
- [Chat UI](docs/prd/CHAT-UI.md)
- [CRM UI](docs/prd/CRM-UI.md)
- [MCP Tools](docs/prd/MCP-TOOLS.md)

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## License

GNU General Public License v3.0 - see [LICENSE](LICENSE)
