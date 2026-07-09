# Personas

**Status:** Complete
**Last Updated:** 2026-07-09
**Audience:** New users

A persona is a personality and scope for the *same* LifeOS assistant. One backend, one tool catalog — several front-of-house characters. A persona changes how LifeOS frames answers, where it looks first, and its tone; it does **not** change what it can do. Every persona keeps the full LifeOS tool suite; they differ only in voice, sourcing, and scope.

Use personas when you want the same underlying assistant to behave differently in different contexts — a terse fitness logger on your phone, an advice-oriented surface grounded in your own notes, a general assistant for everything else.

## Anatomy of a persona file

Personas live in `config/personas/<id>.md`. Each file is an optional YAML **frontmatter** block followed by a **prose body**.

At load time (`settings._parse_persona`) the frontmatter is parsed and stripped, and the body is used **verbatim** as the system-prompt preamble — no template substitution, so the body may safely contain literal `{...}` examples. The YAML never reaches the prompt; it is machine-read config only.

### Frontmatter fields

| Field | Meaning |
|---|---|
| `id` | Should equal the persona's bot `name` in `config/telegram_bots.json` (a mismatch logs a warning). |
| `voice` | A list of behavior rules applied **only on spoken (voice) turns** — speech formatting like "no bullet lists". Ignored on text turns. |
| `model` | **Reserved / no-op today.** It is parsed and stored, but no code path reads it — the orchestrator resolves its model from `LIFEOS_ANTHROPIC_MODEL` plus per-turn escalation, not from this field. Setting it does nothing yet. |

Frontmatter holds **only what code acts on** — never real names, vault paths, or other personal values (the project's open-source rule). A file with no leading `---` block loads whole; a malformed frontmatter block does not crash loading (the loader logs a warning and falls back to the raw file).

### Prose body

The body is the personality the model reads. Not every persona needs every section — include what applies. A common skeleton:

1. **Opening line** — role + surface, one sentence.
2. `## Tone` — personality in text.
3. `## What you do` — core behavior.
4. `## Sourcing` — where to ground answers (omit if nothing special).
5. `## Tools you lean on` — advisory prose about which tools this persona mainly uses (not enforced).
6. `## Response shape` — length/format norms for text.
7. `## When data is thin` — sparse-data fallback.
8. `## Out of scope` — redirect behavior (omit for a catch-all persona).

### Minimal example

A synthetic `config/personas/travel.md` with just enough structure to be useful:

```markdown
---
id: travel
voice:
  - Lead with the answer in one short spoken line.
  - No bullet lists when speaking.
---

You are operating as the **travel bot** — a planning-and-logistics surface of
LifeOS for trips, itineraries, and bookings. You keep the full LifeOS tool
suite; this persona sets your framing and tone.

## Tone

Practical and concise. Give the plan, then the caveats. No filler.

## What you do

Turn a loose request ("weekend somewhere warm in March") into concrete
options with dates, rough costs, and next steps. Offer to add events to the
calendar or draft confirmations when the plan firms up.

## Out of scope

For clearly unrelated requests, answer, then add one line:
_(Your main LifeOS bot is better suited for this.)_ Never refuse.
```

## Built-in personas

LifeOS ships four personas. Each is a full-featured assistant with a different framing.

- **`primary`** — the general-purpose default surface, answering across every domain (knowledge, people, calendar, email, finances, tasks, the web). Concise and direct; offers the obvious next action instead of stopping at the answer. This is the persona used when no other is selected.
- **`therapist`** — an advice-oriented, private surface for personal and relational challenges. Leads with concrete recommendations rather than validation, grounds its answers in your own therapy notes and recent messages with close contacts, routes between individual and relational context, and follows strict privacy rules for the sensitive data it sees. Prefers reading and advising over taking actions.
- **`fitness`** — a clinical, log-first training surface. Records workouts from plain-text messages before reporting back, gives trainer-grade programming and recovery advice on request grounded in your logged data, and periodically refreshes baseline metrics. Terse, no cheerleading.
- **`doctor`** — the self-repair orchestrator (`orchestrates: true`). You message it when LifeOS itself misbehaves; it turns the report into a shipped, tested, reviewed fix through the project's issue and implementation pipeline. Unlike the others, selecting it does not answer inline — it spawns a background session (see below). Details in [doctor-bot.md](doctor-bot.md).

The specialized personas (`fitness`, `therapist`, `doctor`) are bound to Telegram bots in `config/telegram_bots.json`; `primary` is the default and needs no binding.

A persona's sourcing and tone are the levers, not its capabilities: because all four share the full tool suite, the difference between them is entirely which tools they *lean on*, where they look *first*, and how they frame the reply. That is why a persona file is mostly prose — the behavior is instruction, not configuration.

## How personas reach each surface

The persona layer is shared across Telegram, web `/chat`, and voice.

**Telegram.** `config/telegram_bots.json` maps a bot `name` → its `persona_file`. The registry owns *routing* (which token, which chat, whether it `orchestrates`); the persona file owns *behavior*. Frontmatter never duplicates registry fields. A registry entry looks like:

```json
{
  "name": "travel",
  "token_env": "TELEGRAM_TRAVEL_BOT_TOKEN",
  "chat_id_env": "TELEGRAM_TRAVEL_CHAT_ID",
  "persona_file": "config/personas/travel.md"
}
```

Leave a bot's token variable unset to not run it; `*_CHAT_ID` is optional and defaults to the primary chat id. To run a specialized bot, register it in this file and point it at a persona.

**Web `/chat` and voice.** The *same* personas are exposed to the web and voice surfaces through `GET /api/personas`, which lists selectable personas (`primary` plus the configured specialized bots). The web client renders these as a picker (`web/chat/persona.js`); the chosen `persona_id` rides along on `POST /api/ask/stream`, and the server applies the matching persona preamble. On spoken turns (`modality: "voice"`) the server also appends that persona's `voice` frontmatter rules.

**Parity by design.** Selecting a persona in `/chat` is meant to behave **identically** to messaging that persona's Telegram bot — same preamble, same sourcing, same tone. Switching persona in `/chat` starts a fresh, persona-scoped conversation (the sidebar is scoped via `GET /api/conversations?persona_id=<id>`), so threads never bleed across personas.

**Orchestrating personas.** For a persona with `orchestrates: true` (the `doctor` bot), selecting it does not answer inline — the server spawns a background Claude Code session tagged with that bot and streams only an acknowledgement. Results arrive via that bot's Telegram and the `/agents` page. The full contract is in [client-surfaces.md](../specs/technical/client-surfaces.md).

## Create your own persona

1. **Write the persona file.** Add `config/personas/<id>.md` with optional frontmatter and a prose body (see the skeleton and example above). Keep the frontmatter free of personal values; use `<placeholders>` in examples.
2. **(Optional) Bind a Telegram bot.** To reach the persona from a dedicated Telegram bot, register a new bot in `config/telegram_bots.json` with a `name`, its `token_env` / `chat_id_env` variables, and `persona_file: config/personas/<id>.md`. Set `orchestrates: true` only for a self-repair-style pipeline. Bot-token and chat-id setup is covered in [configuration.md](configuration.md#telegram). A persona with no bound bot is still selectable in `/chat` and voice via `GET /api/personas`.
3. **Restart the server** so the loader picks up the new persona and registry entry: `./scripts/server.sh restart`.

The `/routing-health` check verifies `/chat` ↔ Telegram parity — that messaging a persona in `/chat` behaves the same as messaging its Telegram bot, across models and both text and voice. Run it after adding or changing a persona to confirm the surfaces stay in sync.

## Related Documents

### Design Context
- [README.md](../../README.md) — Architecture overview of the two-half (context + agentic) system these personas sit in front of.

### Specifications
- [client-surfaces.md](../specs/technical/client-surfaces.md) — The HTTP contract that maps personas to `/chat`, voice, and Telegram (`GET /api/personas`, `persona_id`, `modality`, orchestrating personas).

### Operational
- [telegram-setup.md](telegram-setup.md) — Registering the primary and per-persona Telegram bots that bind to these personas via `config/telegram_bots.json`.
- [voice-setup.md](voice-setup.md) — Voice mode in `/chat`; a persona's `voice` frontmatter rules apply there too.
- [doctor-bot.md](doctor-bot.md) — The `doctor` self-repair orchestration persona: report a problem → issue → shipped fix.
- [configuration.md](configuration.md#telegram) — Telegram bot tokens, chat ids, and the specialized-bot environment variables that back `config/telegram_bots.json`.
