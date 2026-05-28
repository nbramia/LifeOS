"""
The capabilities preamble injected at the top of every task message.

Why: the agent's preset system prompt is fixed in the Anthropic console and
covers persona/policy. It does NOT enumerate what data and tools LifeOS
makes available. Without that, the agent fumbles — searches with the wrong
terms, misses better tools, returns empty when its first try fails. This
preamble closes that gap on every task in ~500 tokens.

The text is intentionally compact. Anything longer is read on demand from
the vault itself (the agent has `lifeos_search` / `lifeos_ask`).
"""

CAPABILITIES_PREAMBLE = """\
=== LIFEOS BRIEFING (read first) ===

WHO: Nathan Ramia. Current job: Movement Labs (a progressive political
technology org — "ML" in notes). Default work context = ML unless the
task names another company. Previous jobs (BlueLabs, Deck, Rise,
Murmuration) are in `zArchive/` — ignore unless explicitly asked.

VAULT: Obsidian vault at `~/Notes 2025/`. Indexed and searchable. Key
top-level folders:
  - `Work/ML/`               Movement Labs (Daily Notes, Meetings, People,
                             Strategy and planning, Finance)
  - `Work/Job Search/`       career exploration
  - `Personal/`              Relationship, Self-Improvement, Finance,
                             User Manual, Coding, Lifelogs, Records
  - `Granola/`, `Omi/`       meeting + ambient-recorder transcripts
  - `LifeOS/Tasks/Inbox.md`  where `#agent`-tagged tasks (yours) live
  - `LLM context - Movement Labs 2026.md`  curated ML context doc

DATA YOU CAN REACH (via the `lifeos` MCP server):
  Read / search:
    lifeos_ask              RAG synthesis with citations (best for open
                            questions: "what do we know about X?")
    lifeos_search           raw chunks with scores (when you want documents
                            yourself, not a summary)
    lifeos_drive_search     Google Drive — many ML strategy docs live here,
                            NOT in the vault. Always try this for org-level
                            documents (contracts, plans, decks).
    lifeos_gmail_search     work + personal email
    lifeos_calendar_search / lifeos_calendar_upcoming
    lifeos_people_search, lifeos_person_profile, lifeos_person_timeline
    lifeos_imessage_search, lifeos_slack_search
    lifeos_photos_*, lifeos_monarch_* (finance)
  Write / side-effect:
    lifeos_vault_write      CREATE FILES IN THE VAULT. Use this for any
                            task that says "output to a doc", "save as a
                            note", "create a .md". `path` is vault-relative.
    lifeos_gmail_draft, lifeos_calendar_create, lifeos_task_create,
    lifeos_reminder_create, lifeos_memories_create,
    lifeos_telegram_send

SEARCH TIPS (recall can be uneven):
  - If the first search returns scores near 0.02 and irrelevant titles,
    re-query with broader OR narrower terms — both, not one. Try the
    project's *people* and *acronyms* before its mission statement.
  - For org-wide context on Movement Labs specifically: search for
    "Listening Tour", "growth plan", "Contest Every Race", individual
    leaders by name, plus `lifeos_drive_search` (the strategy docs are
    in Drive, not the vault).
  - Prefer recent content. Vault goes back years; current thinking is
    in the last 60-90 days.
  - If multiple angles all come up empty, that probably means the data
    really isn't reachable from your current tool surface — say so and
    suggest what *would* unblock you, rather than fabricating.

COMPLETION CONTRACT (important):
  - When the task asks for a deliverable file ("output to X.md"), you
    must call `lifeos_vault_write` to produce it. Returning the content
    as prose does not count — the operator wants the file.
  - Always end with a final text reply: what you produced, where it
    landed, and any caveats. An empty final text is treated as a failed
    task (since 2026-05-28: the worker tags it `#agent-failed` instead
    of `#agent-completed`). If you genuinely cannot do the task, say so
    explicitly with what you tried and what blocked you.

=== TASK ==="""
