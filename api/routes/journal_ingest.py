"""
Journal ring ingestion endpoint (#660).

Lets a capture device outside the tailnet — the Pebble Index ring is the
motivating case, transcribing speech on-phone and posting the text — feed
fragments into the `journal` persona built in #659. It intentionally does
NOT reimplement capture: `_ingest_fragment` below calls the exact same chat
pipeline entry point (`api.services.telegram.chat_via_api`, with the journal
persona's preamble) that the journal Telegram bot uses for a typed fragment,
so a spoken fragment gets identical treatment — same log file, same task/
schedule thresholds, same ask-when-unsure behavior.

Our own contract (see docs/guides/journal-ring-ingest.md) — the exact shape a
real Pebble Index webhook sends is unknown until the device ships in March
2026 (issue #660 is explicit: build against a contract we control, adapt when
the device is in hand). `_adapt_payload` is the ONLY function that knows the
request body's shape; if a real device's webhook doesn't match, only that
function should need to change.

Auth mirrors the existing bearer-token ingest pattern (`api/routes/fitness.py`
`_check_ingest_auth`, itself modeled on the MCP HTTP transport's
`LIFEOS_MCP_BEARER_TOKEN`): a dedicated token, 503 while unset, 401 on a
missing/wrong bearer, checked BEFORE the body is parsed so an unauthenticated
caller can't probe validation behavior or cause any write.
"""
import hashlib
import hmac
import logging
from datetime import datetime
from typing import NamedTuple, Optional

from fastapi import APIRouter, HTTPException, Request

from api.services.journal_ingest_store import get_journal_ingest_store
from config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/journal", tags=["journal"])

# The journal persona bot's registry name (config/telegram_bots.json) — the
# preamble this resolves to is exactly what a typed Telegram fragment gets.
_JOURNAL_PERSONA_ID = "journal"

# Per-device conversation continuity, mirroring TelegramBotListener's
# per-chat_id `_conversations` map (api/services/telegram.py): in-memory,
# process-lifetime only, one entry per device so consecutive fragments from
# the same ring share conversation context the way consecutive messages in
# the same Telegram chat would.
_conversations: dict[str, str] = {}


class _IngestFields(NamedTuple):
    text: str
    device_id: str
    timestamp: str
    external_id: Optional[str]


def _adapt_payload(body: dict) -> _IngestFields:
    """Extract (text, device_id, timestamp, external_id) from a device
    payload. THE payload adapter — see module docstring. Deliberately does
    not read any "action" field: the device's on-device LLM may include one
    (its guess at create-note/add-reminder/etc), but honoring it would mean a
    spoken fragment and a typed one get interpreted by two different brains.
    The ring is transport; the journal persona (#659) is the interpreter.
    """
    text = str(body.get("text") or "").strip()
    device_id = str(body.get("device_id") or "").strip()
    timestamp = str(body.get("timestamp") or "").strip()
    external_id_raw = body.get("id")
    external_id = str(external_id_raw).strip() if external_id_raw else None
    return _IngestFields(text=text, device_id=device_id, timestamp=timestamp, external_id=external_id)


def _dedupe_key(fields: _IngestFields) -> str:
    """A retried delivery must log once (#660 AC). Prefer a device-supplied
    id when present; otherwise derive the key from the payload itself
    (device_id + timestamp + text) rather than trusting the device to send a
    unique id at all — a flaky-connection retry resends the same three
    values, so the derived key still collapses it to one. Only the derived
    hash (or the external id) is persisted, never the fragment text."""
    if fields.external_id:
        return f"id:{fields.external_id}"
    raw = f"{fields.device_id}|{fields.timestamp}|{fields.text}"
    return "hash:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _check_ingest_auth(request: Request) -> None:
    """Bearer-token gate. Disabled (503) until LIFEOS_JOURNAL_INGEST_TOKEN is
    set — a dedicated token, separate from the MCP transport's and the health
    ingest's."""
    expected = settings.journal_ingest_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Journal ring ingest disabled: set LIFEOS_JOURNAL_INGEST_TOKEN to enable.",
        )
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _parse_timestamp(value: str) -> None:
    """Validate the timestamp parses as ISO 8601. Only used to reject a
    malformed payload cleanly (422) — the log bullet's own HH:MM comes from
    the journal persona's normal "now, local time" convention (same as a
    typed fragment, which carries no separate authored-at time either), not
    from this field. Accepts a trailing 'Z' (fromisoformat pre-3.11 doesn't)."""
    datetime.fromisoformat(value.replace("Z", "+00:00"))


@router.post("/ingest")
async def journal_ring_ingest(request: Request):
    """Ingest a transcribed fragment from a capture device (e.g. a Pebble
    Index ring) into the journal persona's capture log — the same path a
    typed fragment to the journal Telegram bot takes.

    Authenticates and validates BEFORE any write: an unauthenticated or
    malformed request writes nothing, not even a log line containing the
    payload. A previously-seen delivery (same dedupe key) is acknowledged
    without being reprocessed, so a retry logs once.
    """
    _check_ingest_auth(request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="payload must be a JSON object")

    fields = _adapt_payload(body)
    if not fields.text:
        raise HTTPException(status_code=422, detail="text is required")
    if not fields.device_id:
        raise HTTPException(status_code=422, detail="device_id is required")
    if not fields.timestamp:
        raise HTTPException(status_code=422, detail="timestamp is required")
    try:
        _parse_timestamp(fields.timestamp)
    except ValueError:
        raise HTTPException(status_code=422, detail="timestamp is not valid ISO 8601")

    store = get_journal_ingest_store()
    dedupe_key = _dedupe_key(fields)
    if store.was_processed(dedupe_key):
        logger.info(f"Journal ring ingest: duplicate delivery from device {fields.device_id}, not reprocessed")
        return {"status": "duplicate"}

    persona_preamble = settings.resolve_persona(_JOURNAL_PERSONA_ID)
    if persona_preamble is None:
        # The journal bot's token isn't configured (settings.list_http_personas()
        # would omit it) — nothing to route into. Fail closed rather than
        # silently falling back to an unprimed chat turn.
        raise HTTPException(
            status_code=503,
            detail="journal persona is not configured (TELEGRAM_JOURNAL_BOT_TOKEN unset)",
        )

    from api.services.telegram import chat_via_api

    try:
        conversation_id = _conversations.get(fields.device_id)
        result = await chat_via_api(fields.text, conversation_id=conversation_id, persona=persona_preamble)
    except Exception:
        # Never let a fragment (or the underlying error, which may quote it)
        # reach the logs.
        logger.error(f"Journal ring ingest: pipeline error for device {fields.device_id}")
        raise HTTPException(status_code=502, detail="capture pipeline error")

    _conversations[fields.device_id] = result["conversation_id"]
    store.mark_processed(dedupe_key)
    logger.info(f"Journal ring ingest: captured fragment from device {fields.device_id}")
    return {"status": "logged", "reply": result.get("answer", "")}
