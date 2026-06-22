---
name: routing-health
description: On-demand routing health check for /chat. Verifies every model (auto / Sonnet / Opus / Gemma-local / Claude Code) on BOTH text and voice, across every persona, and that messaging a persona in /chat is identical to messaging its Telegram bot.
argument-hint: [--live]
---

# Routing Health Check

Arguments: `$ARGUMENTS`

Modes:
- *(default)* — deterministic dispatch matrix + cheap liveness probes. No cloud cost beyond a single Haiku voice turn; **no Claude Code workers are spawned**. ~30–60s.
- **--live** — additionally runs a real generation per model on text AND voice (verified against the recorded routing model), plus one `claude_code`-via-voice handoff for a no-handoff persona — which spawns a Claude Code worker and then kills it (process included). Real cloud cost (≈ one Opus + one Sonnet + local + Haiku turns, twice). A few minutes.

## Why this exists

The `/chat` model picker (`auto` / `Sonnet` / `Opus` / `Gemma (local)` / `Claude Code`) and the voice surface route a turn through several independent layers: the web/voice client, `model_override` on `/api/ask/stream`, the orchestrator's model resolution + escalation, the whisper-relay voice gateway, and the Claude Code engine handoff. A regression in any one layer — a new picker value with no backend support, a persona that stops resolving, the voice gateway dropping `model_override`, the `claude_code` short-circuit breaking, the handoff gate suppressing a non-handoff persona — is **silent** until someone hits that exact cell. This check exercises the whole grid (2 surfaces × 5 models × every persona) on demand.

It also asserts the design invariant the project depends on: **messaging a persona in `/chat` (text or voice) behaves identically to messaging that persona's Telegram bot** — both resolve the same `bot.persona` preamble from the registry and call the same `run_agent_loop` via `/api/ask/stream`.

## What it checks

- **Inventory** — every model-picker option value in `web/index.html` is one the backend actually handles; enumerates personas from `settings.list_http_personas()`.
- **Persona ↔ Telegram equivalence** — `/api/personas` matches the Telegram bot registry; `resolve_persona(id)` returns each bot's exact `persona` preamble; and *empirically*, `run_agent_loop` receives the **identical** persona whether the turn arrives the `/chat` way (`persona_id`) or the Telegram way (`persona` preamble).
- **Text routing matrix** — every persona × {`auto`, `opus`, `sonnet`, `gemma`, `claude_code`}: the correct model id is resolved (mocked dispatch — **no LLM calls**), `force_local` is set for gemma, the persona preamble is passed through, and `claude_code` short-circuits to a `claude_intent` handoff (no agent loop, no inline answer).
- **Voice forwarding + gate** — whisper-relay forwards `model_override` and bypasses the persona handoff gate for `claude_code` (runs its `tests/test_model_override.py`).
- **Liveness** — the deployed service actually emits `claude_intent` and serves a real voice turn end-to-end.
- **--live only** — a real generation per model on text and voice (asserted against the conversation's recorded routing model), and a live `claude_code`-via-voice handoff for a no-handoff persona (spawned worker + killed, OS process included).

## Instructions

### Step 0: Pre-flight

!`curl -s -m4 http://localhost:8000/health >/dev/null 2>&1 && echo "lifeos-api: up" || echo "lifeos-api: DOWN"; [ -x "$HOME/.venvs/lifeos/bin/python" ] && echo "venv: ok" || echo "venv: MISSING"; pgrep -f "voice_gateway.main" >/dev/null 2>&1 && echo "voice-gateway: up" || echo "voice-gateway: down (voice live-probes will fail)"`

If `lifeos-api` is DOWN, stop and tell the user to start it (`sudo systemctl start lifeos-api`) — every live probe needs it. If the voice gateway is down, the voice liveness probe (and `--live` voice cells) will FAIL; note it but the rest still runs.

### Step 1: Run the checker

Run from the repo root with the project venv. Include `--live` **only if** `$ARGUMENTS` contains `--live`:

```
~/.venvs/lifeos/bin/python .claude/skills/routing-health/scripts/routing_check.py [--live]
```

The script prints a `[PASS]/[FAIL]/[WARN]` line per check, a final `ROUTING-HEALTH-VERDICT:` line, and a `ROUTING-HEALTH-JSON:` summary blob. It **cleans up its own** probe conversations (across every persona scope) and, under `--live`, kills the worker session it spawns (and its OS subprocess). Suppress noisy warnings with `2>/dev/null`.

### Step 2: Report

Interpret the script's output (do **not** re-run the underlying tests yourself). Produce a concise report:

- **Overall** — 🟢 HEALTHY / 🟡 DEGRADED (any WARN) / 🔴 FAILING (any FAIL), taken from the verdict line.
- **Coverage** — state what actually ran: N personas × 5 models on text (always), voice forwarding + gate, liveness, and — if `--live` — the live generation matrix and the `claude_code` voice handoff.
- **Equivalence** — explicitly confirm or deny that `/chat` (text + voice) behaves identically to the Telegram bots for personas (from the `equivalence/*` checks).
- **Failures** — for each FAIL, name the cell and the detail, and point at where to look (table below).
- **Cleanup** — confirm probe conversations were deleted; under `--live`, confirm no stray worker remains (the script's `live-voice/claude_code-cleanup` line, and you may verify with `pgrep -af "claude -p"`).

If the script itself crashed (a `deterministic-harness FAIL` line or a Python traceback), surface that and stop — the matrix didn't run.

### Failure → where to look

| Failing check | Look at |
|---|---|
| `inventory/model-picker-options` | `web/index.html` `#modelPicker` options vs the handled set in `api/routes/chat.py` (`model_override` resolution, ~L1243) |
| `equivalence/*` | `config/settings.py` (`resolve_persona`, `list_http_personas`, telegram bot registry) and `api/services/telegram.py` (`chat_via_api`) |
| `text-matrix/<persona>/<model>` | `api/routes/chat.py` `ask_stream` — model_override resolution + persona handling + the `claude_code` short-circuit |
| `voice/whisper-relay-*` | `~/Code/whisper-relay` adapter (`src/voice_gateway/adapters/lifeos.py`) + `web/chat/voice.js` (model_override on the voice FormData) |
| `live/*`, `live-text/*`, `live-voice/*` | deployed service (restart `lifeos-api`); for voice cells, the whisper-relay gateway on :9788 |

## Notes

- **Read-mostly.** The only writes are throwaway probe conversations (auto-deleted) and, under `--live`, one Claude Code worker session — killed, OS process included (operator-kill alone leaves the process; see issue #379).
- **Requirements:** `lifeos-api` running; for voice checks, the whisper-relay gateway on :9788; for the local/`gemma` cells, `lifeos-llm` active.
- **whisper-relay location** defaults to `~/Code/whisper-relay` — override with `WHISPER_RELAY_DIR`. If it's absent, the voice-forwarding checks WARN (skipped) rather than fail, and the verdict degrades to 🟡 rather than 🔴.
- The deterministic matrix loads the app in-process (TestClient) and mocks `run_agent_loop`, so it makes **no** real LLM calls and spawns nothing — it's safe to run frequently. The cost and side effects live entirely in `--live`.
