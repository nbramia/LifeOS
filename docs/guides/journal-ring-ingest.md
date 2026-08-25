# Journal Ring Ingest — `POST /api/journal/ingest`

> **Audience:** Operator configuring a capture device
> **Status:** Complete
> **Last Updated:** 2026-08-23

Lets a capture device — the motivating case is the [Pebble Index](https://repebble.com/index)
ring, which transcribes speech on-phone and can "route… transcribed text
directly to your own app via webhook" — feed fragments into the `journal`
persona ([#659](../../config/personas/journal.md)) from outside the tailnet.

**This is our own contract, not the ring's.** The Pebble Index ships March
2026; its real webhook payload shape is unknown until then. This endpoint
defines the shape we control and documents it so a device (or a `curl` test)
can be configured against it now. If a real device's webhook doesn't match,
only `_adapt_payload()` in [`api/routes/journal_ingest.py`](../../api/routes/journal_ingest.py)
needs to change — everything else (auth, idempotency, the capture call) is
independent of the payload's exact field names.

## Why a webhook, not direct MCP

`lifeos-mcp-http` (`:8765`) already exposes LifeOS's tools behind a bearer
token and could be called directly by the ring's app. That path is
deliberately **not** used here: it would hand interpretation of a spoken
fragment to the ring's own small on-device model choosing among 60+ tools,
so a spoken thought and a typed one could end up governed by two different
judgments about what deserves a task. This endpoint keeps the ring as
transport and the `journal` persona as the sole interpreter — see #660.

## Behavior

A valid request is handed to **the exact same chat pipeline call** the
`journal` Telegram bot makes for a typed message (`api.services.telegram.
chat_via_api`, primed with the journal persona's preamble). It is not a
separate implementation of capture — same log file
(`Personal/Log/YYYY-MM-DD.md`), same task/schedule extraction thresholds,
same ask-when-unsure behavior described in
[`config/personas/journal.md`](../../config/personas/journal.md).

The write itself is deterministic and done in code
([`api/services/journal_capture.py`](../../api/services/journal_capture.py)),
before the model's turn starts — the fragment survives whether or not the
model does anything useful, and the pipeline reports back that it landed. This
endpoint requires that confirmation before it answers `status: "logged"` or
records the delivery as processed. Prior to #674 it inferred capture from "the
pipeline returned without raising", which reported success for fragments that
were never written and burned their dedupe keys.

One documented difference from a Telegram message: the bullet's `HH:MM`
timestamp is whatever the persona resolves as "now, local time" at
processing time — same as a typed message, which also carries no separate
"composed at" time. The payload's `timestamp` field is used for validation
and idempotency, not injected into the fragment text, so it does not
override the bullet's clock. This only matters if a device buffers a
fragment offline and delivers it late; nothing in the product description
suggests that today.

**The device's on-device LLM may include its own guessed `action`.** It is
read by nobody — the field may be present in a real payload and is
deliberately ignored, for the reason above.

## Enabling it

Set a dedicated bearer token in `.env` (separate from `LIFEOS_MCP_BEARER_TOKEN`
and `LIFEOS_HEALTH_INGEST_TOKEN` — a compromised ring token shouldn't need
rotating the others):

```bash
LIFEOS_JOURNAL_INGEST_TOKEN=$(openssl rand -hex 32)
```

Empty (the default) disables the endpoint — it returns `503`. The `journal`
persona itself must also be configured (`TELEGRAM_JOURNAL_BOT_TOKEN` set,
per [#659](../../config/personas/journal.md)); if it isn't, the endpoint
returns `503` rather than routing to an unprimed chat turn.

Point the device (or its phone-side app) at:
```
https://<your-machine>.<tailnet>.ts.net/api/journal/ingest
```

## Contract

```
POST /api/journal/ingest
Authorization: Bearer <LIFEOS_JOURNAL_INGEST_TOKEN>
Content-Type: application/json

{
  "text": "call a friend this week",       // required, non-empty transcription
  "device_id": "pebble-index-fa12",         // required, identifies the source device
  "timestamp": "2026-08-23T14:37:00Z",      // required, ISO 8601
  "id": "device-local-message-id",          // optional — see Idempotency below
  "action": "reminder"                      // optional, deliberately ignored (see Behavior)
}
```

Response (`200`):
```json
{"status": "logged", "reply": "Logged."}
```
or, for a delivery already seen:
```json
{"status": "duplicate"}
```

| Status | Meaning |
|---|---|
| `200` | Captured (`status: "logged"`) or recognized as a repeat delivery (`status: "duplicate"`) — nothing reprocessed either way. |
| `401` | Missing or wrong bearer token. Nothing written. |
| `422` | Missing/empty `text`, `device_id`, or `timestamp`, or a `timestamp` that isn't valid ISO 8601. Nothing written. |
| `400` | Body isn't valid JSON. Nothing written. |
| `502` | The chat pipeline failed (e.g. the LLM backend is down), or it returned without confirming the fragment reached disk. Nothing captured; not recorded as processed, so an identical retry is *not* treated as a duplicate. |
| `500` | The capture write itself failed. Same as `502` for retry purposes: nothing recorded as processed. |
| `503` | The endpoint is disabled (`LIFEOS_JOURNAL_INGEST_TOKEN` unset) or the `journal` persona isn't configured. |

Auth is checked **before** the body is parsed, so an unauthenticated request
can't probe validation behavior, and a rejected request never reaches the
capture pipeline — no partial entry, no log line containing the payload.

## Idempotency

A retried delivery (flaky connection) must log once. If the payload carries
an `id`, it is used as the dedupe key directly — a device-native identifier
is the most reliable signal when the device provides one. Otherwise the key
is derived from the payload itself: a hash of `device_id + timestamp + text`,
since a retry resends those three values unchanged. Either way, only the
derived key (a hash, or the device's own id) is persisted — never the
fragment text — in `data/journal_ingest.db`.

A delivery is marked processed only **after** the pipeline confirms the
fragment is on disk — not merely after it returns — so a failed attempt can be
retried rather than being silently swallowed as a duplicate forever.

## Verify with `curl`

```bash
curl -X POST http://localhost:8000/api/journal/ingest \
  -H "Authorization: Bearer $LIFEOS_JOURNAL_INGEST_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "idea about the deploy gate", "device_id": "test-device", "timestamp": "2026-08-23T14:37:00Z"}'
```

Check `Personal/Log/<today>.md` in the vault for the new bullet.

## Related Documents

- [`config/personas/journal.md`](../../config/personas/journal.md) — The persona this endpoint routes into (#659); authoritative on task/schedule extraction. Log shape is `api/services/journal_capture.py`'s since #674.
- [Configuration](configuration.md#journal-ring-ingest) — `LIFEOS_JOURNAL_INGEST_TOKEN` reference.
- [`api/routes/journal_ingest.py`](../../api/routes/journal_ingest.py) — Implementation; `_adapt_payload()` is the one function to change once a real device's webhook is observed.
- [`api/services/journal_capture.py`](../../api/services/journal_capture.py) — The deterministic write this endpoint's capture confirmation comes from (#674).
- [Apple Health Import](apple-health.md) — The precedent this mirrors: a dedicated bearer-token ingest endpoint for an external capture device.
