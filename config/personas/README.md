# Persona files

Each persona is a markdown file: an optional YAML **frontmatter** block (machine-read config) followed by the **prose body** (the personality the model reads). The bot registry (`config/telegram_bots.json`) maps a bot `name` → its `persona_file`; this file owns *how the persona behaves*, the registry owns *routing* (token, chat, `orchestrates`). Frontmatter never duplicates registry fields.

The loader (`settings._parse_persona`, used by `settings.telegram_bots`) strips the frontmatter and returns the **body only** as the system-prompt preamble — so the YAML never leaks into the prompt. The body is used **verbatim** (no `str.format`), so a persona may safely contain literal `{...}` examples (e.g. `fitness.md`).

## Frontmatter (YAML, optional)

| Field | Meaning |
|---|---|
| `id` | Should equal the bot `name` in `telegram_bots.json` (a mismatch logs a warning). |
| `model` | **Reserved** — an optional per-persona model preference, parsed and stored on `TelegramBotConfig` but not yet read by any code path (the orchestrator resolves its model from `LIFEOS_ANTHROPIC_MODEL` + per-turn escalation). Setting it is currently a no-op. |
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

## What the loader consumes

The frontmatter is parsed and stripped; the body becomes the system-prompt preamble (used verbatim — no `str.format`). Of the parsed fields:

- `voice` rules are applied on voice (spoken) turns and ignored on text turns.
- `model` is **reserved** — stored but not read by any code path yet (see the field table above).

Beyond the registry personas, `primary.md` carries the primary persona's personality, and a resolved **personal-context block** (partner / therapists / inner circle / folders, drawn from existing config — never hardcoded here) is composed for personas that need it. The schema's main open extension point is a hard tool allow-list (`tools:`), which is not parsed yet.

## Surface-specific variants

A persona's *personality* is usually surface-independent, but its **execution model** sometimes isn't — `doctor` drives a headless Claude Code session (shell, git, `gh`) on Telegram and web, but on Hermes it has only MCP tools (`lifeos_agent_spawn` and friends) and no shell. Rather than branch on surface inside one file, drop a sibling file named `<stem>.<surface><suffix>` next to the default one — e.g. `doctor.md` + `doctor.hermes.md`. `settings.resolve_persona(persona_id, surface=...)` checks for that sibling and falls back to the default file when it's absent, so:

- A persona with no sibling for a given surface resolves identically everywhere — adding the mechanism costs nothing until it's used.
- A new surface-specific persona needs no code change: the file's existence is the whole registration.
- The default file (and everything that reads it without passing `surface`, e.g. the Telegram bot) is completely unaffected by adding a variant elsewhere.

A variant file is a plain body — no frontmatter — since `voice`/`model`/`id` still come from the default file; `resolve_persona` only asks the variant for its prose. Write it as its own complete persona (same philosophy, different mechanics), not a diff against the default.
