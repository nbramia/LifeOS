# Voice Setup

**Status:** Complete
**Last Updated:** 2026-08-26
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
| `LIFEOS_VOICE_ENDPOINT_SILENCE_MS` | `1600` | Smart turn endpointing (below): trailing silence, in ms, after speech before a candidate endpoint check runs. |
| `LIFEOS_VOICE_ENDPOINT_HARD_CAP_MS` | `3000` | Smart turn endpointing: continuous silence, in ms, that finalizes the turn regardless of any completeness verdict. |
| `LIFEOS_VOICE_ENDPOINT_SEMANTIC` | `false` | Reserved for an optional LLM completeness classifier — **not implemented**; flipping it currently has no effect. The heuristic alone governs completeness. |

These three, along with `default_voice`/`secure_url`/the remote-model fields, are read by the web client from `GET /api/chat/config` — the endpointing pair are pure client-side VAD timing knobs with no server-side use; they exist as settings only so that timing is operator-tunable without editing `web/chat/voice.js`.

Restart the API after editing `.env`:

```bash
./scripts/server.sh restart
```

### Installing to a Home Screen (iOS/Android)

`/`, `/chat`, and `/crm` are all standalone-capable and share a web app
manifest (`display: "standalone"`), so adding any of them to a phone's
Home Screen launches without browser chrome instead of opening in the
device's default browser. This matters for voice specifically: a page
running inside the standalone container gets its own microphone-permission
grant, separate from — and not inherited from — the regular browser tab.
On iOS, add the shortcut from Safari (not another default browser) via
Share → Add to Home Screen, from the tailnet HTTPS URL above. A shortcut
added before this feature shipped won't upgrade itself — re-add it.

### Action Button deep link (iPhone Shortcuts) (#731)

An iPhone's Action Button can't call LifeOS directly — it triggers a Shortcut, and the Shortcut opens a URL. `/chat` accepts two independent URL params for this:

- **`?mode=voice`** — puts the page in voice mode. On a cold launch, if the **Listening** dock toggle is on (the default), this only *arms wake-listening*: the page holds a live mic and waits for a spoken wake burst ("Hermes") before it starts an actual recording — see "Listening" below. It does **not** start recording by itself, and never has.
- **`?record=1`** (added for #731) — begins an actual recording immediately on page load, the same code path a manual tap on the talk button uses. It only fires **alongside** `?mode=voice` in the same URL (it has no effect on its own, and `?mode=voice` alone never implies it) and only on the navigation that actually carries it — reloading the page later without the param doesn't replay it. It respects the same secure-context/mic-permission guard as a manual tap: if the mic isn't usable yet (no HTTPS, permission not yet granted, etc.), you get the normal blocked-mic message in the thread instead of a silent hang.

Given the Action Button press already *is* the intent to speak, point the Shortcut at both params together:

```
https://<your-machine>.<your-tailnet>.ts.net/chat?mode=voice&record=1
```

**Shortcuts recipe:** create a new Shortcut with a single **Open URL** action set to the URL above, then assign it to the Action Button (Settings → Action Button → Shortcut → pick it). The very first press still needs a manual mic-permission grant (see below) before later presses can go straight to recording.

**Known limitations — verify on your own device before relying on this:**
- Shortcuts' **Open URL** action may open the link in Safari (a regular browser tab) rather than the installed standalone Home Screen web app, depending on iOS version and whether a matching Home Screen shortcut already exists. If it lands in Safari, you're in a *different* mic-permission container than the standalone app (see "Installing to a Home Screen" above) — the first Action Button press there will need its own permission prompt even if you already granted the standalone app's icon. If Shortcuts offers an **Open App** (or "Open \<App Name\>") action targeting the already-installed Home Screen icon on your device, prefer that over **Open URL** — it's more likely to land in the standalone container consistently, though this hasn't been verified across iOS versions.
- Because the Action Button's press-to-launch isn't a real in-page tap gesture, some browsers may be stricter about a page requesting the microphone with no click behind it. This mirrors the existing Listening feature, which already acquires the mic without a gesture whenever `?mode=voice` loads with Listening on — so this isn't new risk, but it's still worth testing once on your device rather than assuming it works.

## The voice dock in /chat

In voice mode the text composer is replaced by the dock:

- **Tap-to-talk** — the shutter button starts/stops recording; a cancel (`✕`) aborts an in-flight turn.
- **Text/Voice pill** — a two-way pill selector at the top of `/chat` (next to the backend selector) swaps between the text composer and the voice dock. The mode persists per browser.
- Four dock toggles (state saved per browser):
  - **Mute** — suppress spoken playback (the reply still returns as text).
  - **2×** — play spoken replies at double speed.
  - **Auto** — auto-continue: after a reply finishes, start listening for the next turn without another tap. While Auto is on, a recording also **ends itself** once you sound done — see "Smart turn endpointing" below — instead of always waiting for another tap.
  - **Listening** — wake-word mode, default off (#710). While on, the page holds its own mic stream and runs a local energy-based VAD (no third-party wake-word engine, no Web Speech API — that ships audio to Google). When it hears a speech burst end, it POSTs the short clip to `${voice gateway}/api/voice/transcribe` and fuzzy-matches the transcript against "Hermes" (tolerating whisper-isms like "Hermès" or "her mes" — see `matchesWakeWord()` in `web/chat/voice.js`); a match plays a short confirmation chime, then starts recording exactly as a talk-button tap would. Detection is suspended while recording, while a turn is in flight, and while a spoken reply or the chime is playing (so the assistant — or the chime itself — can never wake it), and resumes after. Leaving voice mode or unchecking Listening releases its mic entirely. **Requires a `/api/voice/transcribe` route on the voice gateway that does not exist yet as of this writing** — see the note below.

### The wake-confirmation chime

On a confirmed wake match, Listening plays one randomly-chosen sound from `web/chat/wake-sounds/` — described by `web/chat/wake-sounds/manifest.json` (`{"sounds": ["file1.mp3", "file2.mp3", ...]}`) — before it starts recording, so you get audible confirmation that it heard "Hermes" and is now listening for your actual request. The chime always finishes (or hits its ~1.5s safety timeout) before recording begins, so it's never captured as part of your turn.

Both the sound files and the manifest are optional. If `web/chat/wake-sounds/` or its manifest is missing, empty, or fails to load, Listening falls back to today's behavior with no chime at all — recording starts immediately on a wake match, with no error surfaced. This matters for a from-source build or a stripped-down install that doesn't carry the bundled assets: the capability (`playWakeChime()` in `web/chat/voice.js`) degrades gracefully with nothing to configure.

**Attribution**: the bundled sound set is drawn from the PeonPing community packs — `jarvis-mk2`, `eve-walle`, `d2_deckard_cain`, and `diablo-drops` — licensed **CC-BY-NC-4.0** (non-commercial use, attribution required). This is the sole attribution notice for those files; see the license text for full terms if redistributing.

### Smart turn endpointing

While **Auto** is on, a voice recording no longer only stops when you tap the talk button again — it also infers, mid-recording, when you've likely finished speaking, and ends and sends the turn itself. This runs entirely inside `web/chat/voice.js`, on the **same** recording stream the talk button already acquired (never a second microphone request), so it composes with Listening's own mic hold and wake detection above without conflict.

The pipeline (`checkEndpointCandidate()`/`isTranscriptComplete()`/`handleEndpointFrame()` in `web/chat/voice.js`):

1. **Pause detection.** The same local energy-VAD Listening uses for wake-burst detection runs on the recording. After `LIFEOS_VOICE_ENDPOINT_SILENCE_MS` (default 1600ms) of trailing silence *following speech*, the recording-so-far is a **candidate** endpoint — not a final decision yet.
2. **Transcribe-so-far.** The candidate's audio is POSTed to the same bare-STT route Listening's wake check uses, `POST /api/voice/transcribe` (see the dependency note below) — no conversation/turn artifacts either way.
3. **Cancel check.** Before completeness is even considered, the same transcript is checked against `isCancelUtterance()` (see "Spoken cancel" below). A match discards the recording; nothing else in the pipeline runs.
4. **Completeness heuristic.** `isTranscriptComplete()` treats trailing terminal punctuation (`.`/`?`/`!`) as a finished thought. A trailing conjunction/filler word — "and", "but", "so", "because", "or", "if", "then", "um", "uh", "like", "i mean" — or no terminal punctuation at all reads as still-talking. Whisper often omits punctuation on short clips, so "no punctuation" alone is a soft signal; a false "keep listening" only costs one more candidate check (or the hard cap below), never a hang, so the heuristic is deliberately conservative about calling a turn complete.
5. **Act.** Complete → the turn ends and sends through the exact same path a manual stop-tap uses. Incomplete → recording continues; if you keep talking, the silence timer re-arms for the next pause. Either way, `LIFEOS_VOICE_ENDPOINT_HARD_CAP_MS` (default 3000ms) of *continuous* silence ends the turn regardless of any completeness verdict, so an ambiguous or unreachable check can never leave the mic open forever.

`LIFEOS_VOICE_ENDPOINT_SEMANTIC` is a reserved setting for an optional LLM completeness classifier on ambiguous candidates (no terminal punctuation, no recognized filler word either). It is **not implemented** — there is no server endpoint backing it, and flipping it currently has no effect. The heuristic above is the only completeness signal today.

Endpointing only ever runs while Auto is on, voice mode is active, and a recording is actually in progress; with Auto off, recording behaves exactly as it always has — only a tap ends it.

#### Spoken cancel

While Auto is on, ending a recording with a cancel phrase discards it instead of sending it — say "cancel" (or just stop mid-sentence with "never mind," "forget it," or "scratch that") and the recording stops with nothing submitted, exactly as if you'd tapped the talk button to stop on an empty/silent recording: no turn is created, no reply is generated, and — per the manual-stop rationale above — auto-continue does not re-arm for that cycle. Wake detection and the wake chime are unaffected; the cancel check only ever runs on a candidate-pause transcript while actually recording.

The matcher (`isCancelUtterance()` in `web/chat/voice.js`) is **trailing-anchored**: it normalizes the transcript (lowercase, strip trailing punctuation) and checks whether the *end* of it is one of `cancel`, `cancel that`, `never mind`, `nevermind`, `forget it`, `scratch that`. This is deliberate — a substring match would treat "cancel my 3pm with Dana" as a cancellation, silently eating a legitimate request that happens to contain the word "cancel." Trailing-anchoring means that request is transcribed and submitted normally; only an utterance that *ends with* one of those phrases (most commonly the whole utterance, e.g. "Cancel.") is treated as a cancel.

Cancel detection reuses the exact candidate-pause transcript step 2 above already fetches — there is no separate check interval, timer, or additional STT call for it. Its accuracy and latency are therefore identical to endpointing's own: a cancel is only noticed at the next candidate pause, gated by the same `LIFEOS_VOICE_ENDPOINT_SILENCE_MS`/`LIFEOS_VOICE_ENDPOINT_HARD_CAP_MS` settings above.

### The bare-STT gateway route (not yet shipped)

Both Listening (above) and smart turn endpointing depend on the same **transcribe-only** gateway route, `POST /api/voice/transcribe` — no LLM call, no TTS, no conversation/turn persistence, safe to call every second or two without creating any artifacts. whisper-relay's `POST /api/voice/turn` and `/turn/stream` always run the full STT→LLM→TTS pipeline instead. LifeOS's `/api/voice/*` proxy already forwards any path generically (`api/routes/voice.py`), so once the gateway adds `POST /api/voice/transcribe` (multipart `audio` file in, `{"transcript": "..."}` out — the same audio normalization + `STTAdapter.transcribe()` step `turns.py` already uses internally, minus everything after it), both features pick it up with no LifeOS-side change. Until then, every wake check and endpointing candidate check 404s (`transcribeClip()` in `web/chat/voice.js` treats that as "no transcript" and moves on, not an error), so Listening never actually triggers and an endpointing candidate never gets a "complete" verdict from step 3 above. The hard cap in step 4, though, is a pure client-side silence timer with no dependency on this route — it still fires on schedule either way, so under Auto a recording that goes quiet for `LIFEOS_VOICE_ENDPOINT_HARD_CAP_MS` still ends and sends itself even before the gateway ships this route; it just never ends *early* on a genuinely finished sentence until it does.

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

A spoken turn on the Hermes backend is *eventually* meant to route through LifeOS's **own** Hermes proxy (`POST /api/hermes/ask/stream`) rather than the harness directly, exactly as the browser calls it for a typed Hermes turn — that's the seam where persona resolution and the `lifeos_context` envelope live (see [client-surfaces.md](../specs/technical/client-surfaces.md)). As of this writing the gateway doesn't do that yet (`nbramia/whisper-relay#32`, still open): it calls the Hermes harness directly, so a spoken Hermes turn carries no persona context or spoken-style rules today. Conversation persistence doesn't depend on #32 landing, though — `api/routes/voice.py` tees `POST /api/voice/turn/stream` into the conversation store directly (#711), so a Hermes-backend voice conversation still survives a page refresh even without persona context.

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
| `Listening` never triggers recording | Expected until the gateway ships `POST /api/voice/transcribe` — see above. |
| Auto-mode recording never ends itself on a finished sentence, only on the hard cap or a tap | Expected until the gateway ships `POST /api/voice/transcribe` — see above; the hard cap alone still fires on schedule. |

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
