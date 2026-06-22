# ADR-016: Reverse-Proxy the Voice Gateway Through LifeOS

**Status:** Complete
**Last Updated:** 2026-06-22
**Decision:** Accepted

## Context

LifeOS `/chat` is becoming the single responsive client for both text and voice (#361). Voice *transport* — speech-to-text, TTS, and the turn pipeline — lives in a separate app, whisper-relay (`~/Code/whisper-relay`), which exposes `POST /api/voice/turn/stream` (multipart audio in, SSE turn events out), a cancel endpoint, and audio-clip serving. whisper-relay's companion ADR-005 ("LifeOS-owned chat client") records that LifeOS owns all UI and that whisper-relay stays transport-only.

The browser needs to: capture the microphone (which requires a **secure context** — HTTPS, satisfied by Tailscale), and reach the voice endpoints. The question is how the same-origin LifeOS page reaches whisper-relay, which runs as a separate process on `:9788`.

## Decision

LifeOS **reverse-proxies** `/api/voice/*` to the voice gateway at `LIFEOS_VOICE_GATEWAY_URL` (default `http://127.0.0.1:9788`). The browser only ever calls LifeOS's own origin; LifeOS forwards each request to the gateway and streams the response back unchanged. LifeOS adds **no** voice logic — it is a pass-through (`api/routes/voice.py`).

## Rationale

- **Same-origin = one HTTPS front, one mic permission, no CORS.** `getUserMedia` needs a secure context; the `/chat` page is already served over Tailscale HTTPS. Keeping voice calls same-origin means no CORS preflight, no second TLS endpoint, and one bookmark.
- **Transport-only invariant preserved.** LifeOS forwards bytes; whisper-relay keeps doing exactly what it already does. No LifeOS Python is imported into the voice path and no voice logic is duplicated.
- **Trust model unchanged.** The gateway is unauthenticated and localhost-bound; LifeOS is already the access-control front for localhost/Tailscale. Proxying through LifeOS doesn't widen exposure — the gateway URL is fixed server config (not user-controllable), so there is no SSRF surface.

## Alternatives Considered

### Direct browser → gateway with CORS

The browser calls `http://gateway:9788` directly; the gateway sets CORS headers for the LifeOS/Tailscale origin.

**Rejected because:** it needs a second HTTPS-terminating endpoint for the secure context, CORS configuration on the gateway, and a second origin in the browser — more moving parts for no benefit. Recorded as the documented fallback in whisper-relay#22, not the primary path.

### Fold the voice pipeline into LifeOS

Move STT/TTS/turn logic into LifeOS so there's no separate service.

**Rejected because:** it discards a working, separately-iterated app and violates the transport-only split in ADR-005. whisper-relay's voice pipeline is proven; LifeOS should consume it, not re-implement it.

## Consequences

### Positive

- The unified `/chat` client works for voice with one origin and one mic grant.
- whisper-relay stays independently deployable and testable; LifeOS holds no voice logic.
- The proxy is generic (`/api/voice/{path}`), so new gateway endpoints need no LifeOS change.

### Negative

- LifeOS is now in the voice request path; if `lifeos-api` is down, voice is down (already true for the page itself).
- LifeOS must stream both directions (it does, via httpx `aiter_raw`) and not buffer large uploads (the request body is streamed upstream, not read into memory).
- One more service dependency to run (`whisper-relay` on `:9788`) and one more env var (`LIFEOS_VOICE_GATEWAY_URL`).

## Related Documents

### Design Context
- [whisper-relay ADR-005: LifeOS-owned chat client](https://github.com/nbramia/whisper-relay/blob/main/docs/adr/005-lifeos-owned-chat-client.md) — the cross-repo decision this implements

### Specifications
- [client-surfaces.md](../specs/technical/client-surfaces.md) — the HTTP client surfaces, including the voice reverse-proxy and turn contract

### Code References
- [api/routes/voice.py](../../api/routes/voice.py) — the streaming reverse proxy
- [config/settings.py](../../config/settings.py) — `LIFEOS_VOICE_GATEWAY_URL`
- [tests/test_voice_proxy.py](../../tests/test_voice_proxy.py) — proxy forwarding, error, and guard tests
