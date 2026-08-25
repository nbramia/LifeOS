# Voice Setup

**Status:** Complete
**Last Updated:** 2026-08-25
**Audience:** Operators

This guide sets up **voice mode** in LifeOS. Voice is a tap-to-talk input mode *inside* the web `/chat` client — not a separate app or page. It reaches the same orchestrator, the same personas, and the same conversations as text chat. The speech pipeline (STT and TTS) is provided by a **separate** service, **whisper-relay**; LifeOS only reverse-proxies it and adds the browser UI.

---

## What voice is

Voice mode adds a tap-to-talk dock to `/chat`. A turn flows:

```
mic audio → whisper-relay STT → LifeOS orchestrator (/api/ask/stream) → whisper-relay TTS → spoken reply
```

whisper-relay is an **API-only transport** — it captures no UI. LifeOS is the single client. The browser only ever talks to LifeOS's own origin: LifeOS **reverse-proxies** `/api/voice/*` to the gateway so the page stays **same-origin**, which is required because the microphone (`getUserMedia`) needs a **secure context (HTTPS)**. See [ADR-016](../adr/016-voice-gateway-reverse-proxy.md).

whisper-relay is a separate app in its own repository — it is **not** installed by this repo, and its install/run steps are **not** documented here. Run it as a localhost service per its own README; this guide covers only the LifeOS-side configuration and the HTTPS front it requires.

## Parity with text chat

Anything text chat can do, voice can do — because both hit the **same** `POST /api/ask/stream`:

- **Same personas.** The persona picker in `/chat` is shown in voice mode too. Voice sends the chosen `persona_id`; the server applies the matching persona and, on a spoken turn, appends that persona's `voice` frontmatter rules to the system prompt. Those rules are speech-formatting norms only (for example: speak in plain sentences, keep it short, don't read out URLs or file paths) — each persona file defines its own; see [personas.md](personas.md).
- **Same per-turn model picker.** `Auto` / `Sonnet` / `Opus` / `Gemma (local)` / `Claude Code`. Voice forwards the same `model_override` the text picker uses. The picker is shown in voice mode as well — it is hidden on the Agent and Hermes backends (below), both of which ignore model picks (Hermes still shows the persona picker; Agent hides that too).
- **Same conversations.** Voice and text share the persona-scoped thread sidebar and conversation history.

## Setup (LifeOS side)

Three moving parts: the gateway on localhost, the HTTPS front, and the LifeOS env vars.

### 1. Run whisper-relay on localhost

Start the whisper-relay service so it listens on `http://127.0.0.1:9788` (its default). LifeOS reaches it there. Follow whisper-relay's own documentation for installation and startup — those steps live in that repo, not here.

### 2. Expose LifeOS over HTTPS (same-origin, secure context)

The microphone requires HTTPS. On Tailscale, the tailnet provides a valid certificate; the setup script fronts LifeOS on the tailnet HTTPS endpoint (port 443) and proxies to the local API:

```bash
# Generate and install the user unit that runs the Tailscale HTTPS front
./scripts/install-systemd-tailscale.sh
systemctl --user enable --now lifeos-tailscale.service
```

`install-systemd-tailscale.sh` writes a user systemd unit (`lifeos-tailscale.service`) that waits for the local API to report healthy, then runs `scripts/setup-tailscale.sh` — which calls `tailscale serve` to publish LifeOS on the tailnet HTTPS front. After this, open `/chat` at your tailnet HTTPS URL (`https://<your-machine>.<your-tailnet>.ts.net/chat`); voice will not work over plain `http://` or a bare LAN IP.

`TAILNET_HTTPS_URL` is an **optional** env var. `scripts/setup-tailscale.sh` and `scripts/server.sh` echo it as a bookmark hint, and `GET /api/chat/config` returns it as `secure_url` so the web client can offer a one-tap **Open over HTTPS** link when the mic is blocked by an insecure context. Leave it unset and that link is simply omitted — nothing else changes.

### 3. Configure LifeOS env vars

These variables are read by `config/settings.py` (aliases shown). They are **not** listed in `.env.example` — add them to your `.env` as needed:

| Variable | Default | Effect |
|----------|---------|--------|
| `LIFEOS_VOICE_GATEWAY_URL` | `http://127.0.0.1:9788` | whisper-relay base URL that LifeOS reverse-proxies `/api/voice/*` to. Override only if the gateway runs on a different local port. |
| `LIFEOS_CHAT_DEFAULT_VOICE` | `false` | Makes voice the **default** `/chat` input mode. Left off so a fresh clone with no gateway isn't dropped onto a non-functional dock. A `?mode=` URL param or a stored per-browser preference still overrides it. |

Restart the API after editing `.env`:

```bash
./scripts/server.sh restart
```

## The voice dock in /chat

In voice mode the text composer is replaced by the dock:

- **Tap-to-talk** — the shutter button starts/stops recording; a cancel (`✕`) aborts an in-flight turn.
- **Text/Voice pill** — a two-way pill selector at the top of `/chat` (next to the backend selector) swaps between the text composer and the voice dock. The mode persists per browser.
- Three dock toggles (state saved per browser):
  - **Mute** — suppress spoken playback (the reply still returns as text).
  - **2×** — play spoken replies at double speed.
  - **Auto** — auto-continue: after a reply finishes, start listening for the next turn without another tap.

Each spoken response bubble is also **tap-to-replay** — tap it to hear the reply again. (Replay is a per-response affordance, not a dock toggle.) Empty or silent recordings are **skipped automatically** by silence detection — there is no manual "skip silent" control.

### Optional Agent and Hermes text backends

`/chat` carries a backend selector — **LifeOS | Agent | Hermes** — where Agent and Hermes each only appear once configured server-side. Both are separate text backends that speak the same `/api/ask/stream` contract; LifeOS proxies each and injects its bearer token server-side so it never reaches the browser (the same generalized proxy factory backs both, #587). The Agent backend has **no personas and no handoff**, so the persona picker and model picker are hidden while it's active. Hermes keeps the persona picker visible but hides the per-turn model picker — model selection there is the harness's concern, not LifeOS's. Configure them with:

| Variable | Default | Effect |
|----------|---------|--------|
| `LIFEOS_AGENT_BACKEND_URL` | *(empty)* | Agent backend base URL. Empty disables the Agent option entirely. |
| `LIFEOS_AGENT_BACKEND_TOKEN` | *(empty)* | Optional bearer token, added server-side. |
| `LIFEOS_HERMES_BACKEND_URL` | *(empty)* | Hermes backend base URL. Empty disables the Hermes option entirely. |
| `LIFEOS_HERMES_BACKEND_TOKEN` | *(empty)* | Optional bearer token, added server-side. |

When Hermes is configured and there's no stored backend preference yet, `/chat` defaults to it (falling back to LifeOS if the availability check fails or times out); an explicit choice — including LifeOS — always wins over that default.

An **orchestrating** persona (below) stays selectable on Hermes and works there on both text and voice: Hermes drives its own background Claude Code worker for that persona (`lifeos_agent_spawn`) instead of answering inline, conversing with you as it triages, spawns, and supervises. This is deliberately different from the same persona on the LifeOS backend, where it spawns a fire-and-forget session and reports back later via Telegram/`/agents` with no mid-flight visibility — see [client-surfaces.md](../specs/technical/client-surfaces.md) for why both are kept rather than one replacing the other. The web client's pending-question polling (for a LifeOS-spawned session's `[CLARIFY]`/`[GOAL]`) only ever starts for a LifeOS-backend orchestrating turn — a Hermes-backend one has no LifeOS-linked session to poll for, since Hermes handles the whole exchange itself.

A spoken turn on the Hermes backend routes through LifeOS's **own** Hermes proxy (`POST /api/hermes/ask/stream`) rather than the harness directly — the gateway is expected to call that endpoint for the Hermes backend, exactly as the browser calls it for a typed Hermes turn. That's the seam where persona resolution, the `lifeos_context` envelope, and conversation persistence all live (see [client-surfaces.md](../specs/technical/client-surfaces.md)); reaching the harness directly would skip all three. A gateway not yet updated to route this way (`nbramia/whisper-relay#32`) must fail the turn visibly with its own error — never silently answer without a persona or the spoken-style rules it would otherwise carry.

Like the voice vars, these live in `config/settings.py` and are not in `.env.example`.

## Known limitation: orchestrating personas don't stream back into voice

Selecting an **orchestrating** persona (for example the `doctor` self-repair bot, `orchestrates: true`) on the **LifeOS** backend does **not** answer inline. The server spawns a background Claude Code session and streams only an acknowledgement. You can still **answer** that session's follow-up questions from web/voice (the conversation exposes a `pending_question` you can reply to). But the session's **results** currently surface via that bot's **Telegram** thread and the **`/agents`** page — they do **not** yet stream back into the web/voice conversation. Treat orchestrating personas over voice as fire-and-then-check-elsewhere, not a full spoken round-trip. Streaming results back into the web thread is a tracked gap in [client-surfaces.md](../specs/technical/client-surfaces.md).

On the **Hermes** backend an orchestrating persona's spoken turn never spawns anything in the first place (see above) — there is no session to answer or check on.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| "Mic blocked — this page is not on HTTPS" | Not on HTTPS. If `TAILNET_HTTPS_URL` is set, the message carries an **Open over HTTPS** link to this same page on that origin — tap it. Otherwise reopen `/chat` on your tailnet HTTPS URL, not `http://` or a LAN IP, and confirm `lifeos-tailscale.service` is active. |
| "Mic unavailable — …" (no microphone API / no MediaRecorder / no supported audio format) | Not an HTTPS problem: the browser itself lacks a recording capability. Use a current Chrome, Safari, or Firefox; in-app webviews and stripped-down browsers often omit these. |
| Dock present but turns error immediately | whisper-relay isn't running on `LIFEOS_VOICE_GATEWAY_URL` (default `:9788`). Start the gateway; check `curl http://127.0.0.1:9788` locally. |
| Voice dock never appears | Voice is opt-in per browser. Tap **Voice** on the Text/Voice pill, or set `LIFEOS_CHAT_DEFAULT_VOICE=true` and restart the API. |
| `Agent` toggle missing | Expected unless `LIFEOS_AGENT_BACKEND_URL` is set. |
| `Hermes` toggle missing | Expected unless `LIFEOS_HERMES_BACKEND_URL` is set. |

---

## Related Documents

### Design Context
- [ADR-016: Reverse-Proxy the Voice Gateway Through LifeOS](../adr/016-voice-gateway-reverse-proxy.md) — Why voice is same-origin reverse-proxied rather than a direct browser→gateway call

### Specifications
- [Client Surfaces](../specs/technical/client-surfaces.md) — The HTTP voice-turn contract, persona/model contract, and the orchestrating-persona result-streaming gap

### Operational
- [Configuration](configuration.md) — Environment variable reference
- [Personas](personas.md) — The persona layer shared by web chat and voice, including per-persona `voice` formatting rules
- [Installation](installation.md) — Config-only second-user setup checklist that references the Agent/Hermes backend variables documented here

### Project Context
- [README](../../README.md) — Architecture overview, including the whisper-relay voice gateway in the service topology
