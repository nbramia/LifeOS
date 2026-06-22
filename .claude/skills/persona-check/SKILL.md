---
name: persona-check
description: Health-check the LifeOS persona layer (files load, /chat matches the Telegram bots per persona, no personal values leak into committed persona files, frontmatter valid) and open every persona doc in Sublime for editing.
argument-hint: [--no-open]
---

# Persona Check

Arguments: `$ARGUMENTS`

A fast, on-demand checkup of the **persona layer** — and a one-command way to pull every persona doc up in Sublime to edit. Lighter than `/routing-health` (which exercises the full model × surface × persona matrix): this needs only settings + one HTTP call, no app load, so it's quick.

Pass `--no-open` to run the checks **without** launching Sublime (e.g. headless / over SSH).

## What it checks

- **File health** — every bot's `persona_file` exists, is readable, and resolves to a non-empty preamble (`primary` legitimately has none today).
- **/chat ⇄ Telegram equivalence** — `/api/personas` matches the bot registry; `resolve_persona(id)` equals the matching Telegram `bot.persona`; advertised `capabilities` match each bot's `orchestrates` flag. (So messaging a persona in `/chat` behaves identically to messaging its bot.)
- **Privacy guard** — none of the *configured* personal values (`LIFEOS_PARTNER_NAME`, `LIFEOS_THERAPIST_PATTERNS`, `LIFEOS_PERSONAL_RELATIONSHIP_PATTERNS`, `user_name`, and `people_dictionary.json` names) — nor absolute home paths — appear in the **committed** persona files. Catches a real name or personal path leaking into the open-source repo.
- **Frontmatter validation** (forward-compatible) — if a persona file carries YAML frontmatter, `id` must match the filename, `voice` must be a list, `model` a string. Silent when there's no frontmatter yet.

## Instructions

### Step 0: Pre-flight

!`curl -s -m4 http://localhost:8000/health >/dev/null 2>&1 && echo "lifeos-api: up" || echo "lifeos-api: DOWN (the equivalence check will WARN, the rest still runs)"; [ -x "$HOME/.venvs/lifeos/bin/python" ] && echo "venv: ok" || echo "venv: MISSING"`

### Step 1: Run the check

From the repo root, with the project venv:

```
~/.venvs/lifeos/bin/python .claude/skills/persona-check/scripts/persona_check.py
```

It prints a `[PASS]/[FAIL]/[WARN]/[INFO]` line per check, a `PERSONA-CHECK-VERDICT:` line, and a `PERSONA-CHECK-JSON:` summary. Read-only — it touches no files and loads no model.

### Step 2: Open the persona docs for editing

Unless `$ARGUMENTS` contains `--no-open`, open every persona doc in Sublime so they're ready to edit:

```
subl config/personas/*.md
```

(If `subl` isn't on PATH or you're headless, note it and skip — the checks still stand.)

### Step 3: Report

Interpret the script output (don't re-run the checks yourself):

- **Overall** — 🟢 HEALTHY / 🟡 DEGRADED (any WARN) / 🔴 FAILING (any FAIL), from the verdict line.
- **Findings** — list any FAIL/WARN with its detail and where to look (table below). Mention that the docs are open in Sublime (or why they aren't).
- If everything passes, say so in a line and note how many personas/files were checked.

### Failure → where to look

| Failing check | Look at |
|---|---|
| `file/<persona>` | `config/telegram_bots.json` (`persona_file` path) and `config/personas/<persona>.md` |
| `equivalence/*` | `config/settings.py` (`resolve_persona`, `list_http_personas`, `telegram_bots`) — a registry/loader drift |
| `privacy/no-personal-values` | the named committed persona file — move the real name/path out into config (`.env` / `relationship_overrides.json`) and refer to it by role |
| `schema/frontmatter` | the named persona file's YAML frontmatter (`id` must equal the filename; `voice` a list; `model` a string) |

## Notes

- **Read-only and cheap.** Settings + one HTTP call; no app/TestClient, no model load, no probe conversations. Safe to run anytime.
- Requires `lifeos-api` running only for the equivalence check (degrades to WARN otherwise).
- For the deeper guarantee — that every model routes correctly across text **and** voice for every persona — use `/routing-health` (or `/routing-health --live`).
