# LifeOS

Self-hosted AI assistant that indexes your digital life for semantic search and synthesis.

---

## Features

- **Semantic + keyword hybrid search** across Obsidian notes, emails, messages
- **Personal CRM** with entity resolution across all data sources
- **Task management** with Obsidian Tasks integration and natural language creation
- **Meeting prep briefings** with relevant context and history
- **People intelligence** - relationship tracking and network visualization
- **MCP server** for Claude Code integration
- **Local-first** - all data stays on your machine

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

### System Overview

```mermaid
flowchart LR
    subgraph Sources["Data Sources"]
        Gmail
        Calendar
        iMessage
        Slack
        WhatsApp
        Vault["Obsidian Vault"]
        Contacts["Apple Contacts"]
        Photos["Apple Photos"]
        LinkedIn
        Phone["Phone Calls"]
    end

    subgraph Processing["Processing Layer"]
        Sync["5-Phase Daily Sync"]
        ER["Entity Resolution"]
        Search["Hybrid Search"]
        Router["Query Router"]
    end

    subgraph Storage["Storage Layer"]
        ChromaDB["ChromaDB\n(vectors)"]
        SQLite["SQLite\n(BM25 + CRM)"]
        JSON["JSON Files\n(entities)"]
    end

    subgraph Outputs["User Interfaces"]
        Chat["Chat UI"]
        CRM["CRM UI"]
        MCP["MCP Tools"]
        Tasks["Task Manager"]
    end

    Sources --> Sync
    Sync --> ER
    ER --> Storage
    Storage --> Search
    Search --> Router
    Router --> Outputs
```

### How Data Flows

Data moves through LifeOS in a clear pipeline from sources to user-facing features:

```mermaid
flowchart TB
    subgraph Collection["Phase 1: Data Collection"]
        direction LR
        G[Gmail] & C[Calendar] & I[iMessage] & S[Slack] & W[WhatsApp]
    end

    subgraph Entity["Phase 2-3: Entity Processing"]
        direction TB
        SE["SourceEntity\n(raw observations)"]
        PE["PersonEntity\n(canonical records)"]
        SE -->|"email/phone match"| PE
        SE -->|"fuzzy name match"| PE
    end

    subgraph Index["Phase 4: Indexing"]
        direction LR
        Vec["Vector Embeddings\n(ChromaDB)"]
        BM["Keyword Index\n(BM25/FTS5)"]
    end

    subgraph Query["Query Pipeline"]
        direction TB
        Q["User Query"]
        R["Query Router\n(Ollama)"]
        VS["Vector Search"]
        KS["Keyword Search"]
        RRF["RRF Fusion"]
        Syn["Claude Synthesis"]

        Q --> R
        R --> VS & KS
        VS & KS --> RRF
        RRF --> Syn
    end

    Collection --> Entity
    Entity --> Index
    Index --> Query
```

### Two-Tier Entity Model

The CRM uses a two-tier model to handle data from multiple sources:

```mermaid
flowchart TB
    subgraph Sources["Raw Observations"]
        GS["Gmail SourceEntity\njohn@company.com"]
        CS["Calendar SourceEntity\nJohn Smith (attendee)"]
        IS["iMessage SourceEntity\n+1-555-123-4567"]
        SS["Slack SourceEntity\njohn.smith@company.com"]
    end

    subgraph Resolution["Entity Resolution Algorithm"]
        direction TB
        E1["1. Exact email match"]
        E2["2. Exact phone match (E.164)"]
        E3["3. Fuzzy name match\n(RapidFuzz token_set_ratio)"]
        E1 --> E2 --> E3
    end

    subgraph Canonical["PersonEntity (Canonical)"]
        PE["John Smith\nEmails: john@company.com\nPhones: +1-555-123-4567\nSources: gmail, calendar, imessage, slack"]
    end

    Sources --> Resolution
    Resolution --> Canonical
```

**Why two tiers?**
- **SourceEntity**: Preserves original data from each source (immutable audit trail)
- **PersonEntity**: Single unified record per person with merged data from all sources
- One person can have 50,000+ source entities across Gmail, Calendar, messages, etc.

### Search Pipeline

Queries go through a hybrid search combining semantic and keyword matching:

```mermaid
flowchart LR
    Q["Query"] --> NE["Name Expansion\n(nicknames → canonical)"]

    NE --> VS["Vector Search\n(ChromaDB)"]
    NE --> KS["BM25 Search\n(SQLite FTS5)"]

    VS --> RRF["RRF Fusion\nscore = Σ 1/(60 + rank)"]
    KS --> RRF

    RRF --> Boost["Boosting\n• Recency (0-50%)\n• Filename match (2x)"]

    Boost --> Results["Ranked Results"]
```

<details>
<summary><strong>Sync Phases (Daily 3AM)</strong></summary>

The unified daily sync runs in 5 phases with dependencies:

```mermaid
flowchart TB
    subgraph P1["Phase 1: Data Collection"]
        direction LR
        G1[Gmail] & C1[Calendar] & L1[LinkedIn] & Co[Contacts] & Ph[Phone] & WA[WhatsApp] & IM[iMessage] & Sl[Slack]
    end

    subgraph P2["Phase 2: Entity Processing"]
        direction LR
        LinkSlack["Link Slack\n(by email)"]
        LinkiMessage["Link iMessage\n(by phone)"]
        SyncPhotos["Sync Photos\n(face recognition)"]
    end

    subgraph P3["Phase 3: Relationship Building"]
        direction LR
        Discovery["Relationship\nDiscovery"]
        Strengths["Calculate\nStrengths"]
    end

    subgraph P4["Phase 4: Vector Indexing"]
        direction LR
        VaultReindex["Vault Reindex\n(ChromaDB + BM25)"]
        CRMIndex["CRM Vectorstore"]
    end

    subgraph P5["Phase 5: Content Sync"]
        direction LR
        GDocs["Google Docs"]
        GSheets["Google Sheets"]
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
flowchart TB
    Q["User Query"] --> Router["Query Router\n(Ollama LLM)"]

    Router -->|"General knowledge"| Direct["Claude Direct\n(no data fetch)"]
    Router -->|"Current events"| Web["Web Search\n(Claude web_search)"]
    Router -->|"Personal data"| Personal["Source Routing"]
    Router -->|"Compound"| Compound["Multiple Actions"]

    Personal --> Sources["Route to sources:\nvault, email, calendar,\nimessage, slack, crm"]
    Sources --> Hybrid["Hybrid Search"]
    Hybrid --> Synthesis["Claude Synthesis"]

    Compound --> Web
    Compound --> Personal

    Direct --> Response["Response"]
    Web --> Response
    Synthesis --> Response
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
flowchart TB
    subgraph Critical["Critical (No Fallback)"]
        ChromaDB["ChromaDB"]
        Embed["Embedding Model"]
        Vault["Vault Filesystem"]
    end

    subgraph Degradable["Degradable (With Fallback)"]
        Ollama["Ollama"] -->|"fallback"| Haiku["Haiku LLM"]
        Haiku -->|"fallback"| Pattern["Pattern Matching"]

        BM25["BM25 Index"] -->|"fallback"| VecOnly["Vector-Only Search"]

        GCal["Google Calendar"] -->|"fallback"| Cached1["Cached Data"]
        Gmail["Google Gmail"] -->|"fallback"| Cached2["Cached Data"]
    end

    subgraph Info["Info Level"]
        Telegram["Telegram"] -->|"fallback"| EmailOnly["Email-Only Alerts"]
    end

    style Critical fill:#ffcccc
    style Degradable fill:#fff3cd
    style Info fill:#d4edda
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
