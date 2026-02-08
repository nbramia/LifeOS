# LifeOS

**Your personal knowledge graph, built from the digital exhaust of your life.**

LifeOS is a self-hosted AI assistant that connects to your email, calendar, messages, notes, and contacts—then makes all of it searchable and queryable through natural language. Ask "What did Sarah and I discuss about the project last month?" and get an answer synthesized from Gmail threads, calendar meetings, Slack DMs, and your Obsidian notes.

The system solves a fundamental problem: the same person appears differently across every platform. Your colleague is `john.smith@company.com` in Gmail, `John Smith` in Calendar invites, `+1-555-1234` in iMessage, and `@jsmith` on Slack. LifeOS automatically resolves these fragments into unified person records, building a personal CRM that tracks your relationships across all channels.

Everything runs locally on your Mac. Your data never leaves your machine—only query synthesis calls the Claude API. A nightly sync pulls from your data sources, indexes everything for hybrid search (semantic + keyword), and keeps your knowledge graph fresh.

---

## What You Can Do

- **Ask questions about your life**: "When did I last talk to Mom?" / "What's the context for my meeting with Acme Corp tomorrow?"
- **Search across everything**: Hybrid semantic + keyword search across notes, emails, messages, calendar
- **Track relationships**: See interaction history, communication patterns, relationship strength with anyone
- **Manage tasks naturally**: "Remind me to follow up with John next Tuesday" creates an Obsidian task
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

### Entity Resolution

The same person appears differently across every data source. Entity resolution automatically links these fragments:

```
 Gmail: john@acme.com        ┐
 Calendar: "John Smith"      │      ┌─ John Smith ──────────────┐
 iMessage: +1-555-0123       ├──────│  john@acme.com            │
 Slack: @jsmith              │      │  +1-555-0123              │
 LinkedIn: John Smith, Acme  ┘      │  847 interactions         │
                                    │  Last: yesterday          │
 = 5 "strangers"                    └─ 1 unified person ────────┘
```

Resolution matches by: **email** (exact) → **phone** (normalized) → **name** (fuzzy). Raw observations are preserved as immutable SourceEntity records (~125k), while merged PersonEntity records (~3.5k) power the CRM.

### Search Pipeline

Queries go through a hybrid search combining semantic and keyword matching:

```mermaid
flowchart LR
    Q["Query"] --> NE["Name Expansion\n(nicknames → canonical)"]

    NE --> VS["Vector Search\n(local embedding model)"]
    NE --> KS["BM25 Search\n(SQLite FTS5)"]

    VS --> RRF["RRF Fusion\nscore = Σ 1/(k + rank)"]
    KS --> RRF

    RRF --> Boost["Boosting\n• Recency\n• Filename match"]

    Boost --> Syn["Synthesis\n(Anthropic API)"]
```

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

**Why phases matter:**
1. Data Collection must complete before Entity Processing can link records
2. Entity Processing must complete before Relationship Building has linked entities
3. Relationship Building must complete before Vector Indexing has fresh CRM data
4. Content Sync runs last (indexed on next cycle)

</details>

<details>
<summary><strong>Query Routing</strong></summary>

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
