# Persona files

Each persona is a markdown file: an optional YAML **frontmatter** block (machine-read config) followed by the **prose body** (the personality the model reads). The bot registry (`config/telegram_bots.json`) maps a bot `name` → its `persona_file`; this file owns *how the persona behaves*, the registry owns *routing* (token, chat, `orchestrates`). Frontmatter never duplicates registry fields.

The loader (`settings._parse_persona`, used by `settings.telegram_bots`) strips the frontmatter and returns the **body only** as the system-prompt preamble — so the YAML never leaks into the prompt. The body is used **verbatim** (no `str.format`), so a persona may safely contain literal `{...}` examples (e.g. `fitness.md`).

## Frontmatter (YAML, optional)

| Field | Meaning |
|---|---|
| `id` | Should equal the bot `name` in `telegram_bots.json` (a mismatch logs a warning). |
| `model` | Optional per-persona model preference (e.g. `opus`); empty/omitted = orchestrator default + normal escalation. |
| `voice` | A list of behaviour rules applied **only on voice (spoken) turns**; ignored for text. |

The frontmatter holds **only what code acts on** — never real names, vault paths, or other personal values (the project's open-source rule; `/persona-check` enforces it). The schema leaves room to add hard tool allow-lists later (a `tools:` field) without restructuring.

A file with no leading `---` block loads whole — frontmatter is purely additive. A file that *starts* with a `---…---` block is always parsed as frontmatter (standard YAML-frontmatter semantics), so don't open a persona body with a raw `---` horizontal rule. A malformed frontmatter block doesn't crash loading — the loader logs a warning and falls back to using the raw file as the preamble.

## Prose skeleton

Not every persona needs every section — include what applies. Suggested order:

1. **Opening line** — role + surface, one sentence.
2. `## Tone` — personality in text.
3. `## What you do` — core behaviour (for `doctor`, the pipeline).
4. `## Sourcing` — where to ground answers (omit if nothing special).
5. `## Tools you lean on` — advisory prose: which tools this persona mainly uses (not enforced).
6. `## Response shape` — length/format norms for text (voice norms go in frontmatter `voice`).
7. `## When data is thin` — sparse-data fallback.
8. `## Out of scope` — redirect behaviour (omit for `primary`, the catch-all).

## Status / roadmap

This file documents the schema as loaded today (frontmatter parsed + stripped; `voice`/`model` stored on `TelegramBotConfig`). Consuming the parsed fields and the rest of the schema is tracked in #390:

- `voice` rules are parsed now but **applied on voice turns** in a follow-up (needs a text-vs-voice modality flag).
- `model` preference is parsed now; wiring it into model selection is a follow-up.
- A `primary.md` (lifting `primary`'s personality out of the static prompt) and a resolved **personal-context block** (partner / therapists / inner circle / folders, from existing config — never hardcoded here) are follow-up phases.
