"""Hermes text-backend proxy (#587) + persona/modality/turn envelope (#590, #591).

`/chat`'s third text backend: Hermes, an agent harness running as a gateway
(same box or reached over the tailnet), which speaks the same `/api/ask/stream`
SSE contract as LifeOS and the Agent backend. LifeOS proxies it at
``POST /api/hermes/ask/stream``, **adding the token server-side** so it never
reaches the browser. Empty ``LIFEOS_HERMES_BACKEND_URL`` disables it entirely —
`GET /api/hermes/status` then reports unavailable and `/chat` behaves exactly as
it does today.

Unlike the Agent backend, Hermes has no way to resolve a LifeOS persona id or
the current per-turn context (date/time, task tags, etc.) on its own, so this
route resolves both here and attaches the result to the forwarded body as a
`lifeos_context` envelope (the cross-repo contract pinned on issue #590,
extended with a `turn` sibling by #591) before forwarding. That means this
route buffers the request body instead of streaming it straight through — the
only place that happens among the text-backend proxies. The status/
bearer-injection/streaming-response logic is otherwise shared with the Agent
backend via `make_backend_router()` in `_proxy.py`.
"""

import json
import logging

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

# Imported (not just re-exported through _proxy.py) so tests can monkeypatch
# `hermes_proxy.settings.hermes_backend_url` / `hermes_backend_token` directly —
# `settings` is a shared singleton, so the factory in `_proxy.py` sees the same
# mutated object.
from config.settings import settings  # noqa: F401

from api.routes._proxy import TIMEOUT, make_backend_router
from api.routes.chat import AskStreamRequest
from api.services.agent_system_prompt import build_turn_context

logger = logging.getLogger(__name__)


def _client() -> httpx.AsyncClient:
    """httpx client for the Hermes backend (a seam for tests)."""
    return httpx.AsyncClient(timeout=TIMEOUT)


def _build_envelope(raw_body: bytes) -> bytes:
    """Attach `lifeos_context` to the forwarded body, resolving the persona.

    Runs before the request reaches httpx, so a malformed body or a bad
    persona is a clean 400 — mirroring the ordering `ask_stream` in
    `api/routes/chat.py` uses for the native path (resolve persona before the
    stream opens). Every field the browser sent is preserved untouched; this
    adds exactly one top-level key.
    """
    try:
        data = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    # Reuse the native request model's validation (attachment size/type caps,
    # persona length bound) rather than reimplementing it — see AskStreamRequest
    # in api/routes/chat.py.
    try:
        parsed = AskStreamRequest.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid request: {exc}")

    # Same registry-backed resolution the native /api/ask/stream uses, and the
    # same default-to-primary behavior when no persona_id is sent.
    persona_id = parsed.persona_id or "primary"
    preamble = settings.resolve_persona(persona_id)
    if preamble is None:
        raise HTTPException(status_code=400, detail=f"Unknown persona_id: {persona_id!r}")
    if settings.persona_orchestrates(persona_id):
        # Orchestrating personas (e.g. doctor) drive a background Claude Code
        # session — a LifeOS-native capability Hermes doesn't have. Routing
        # those turns to LifeOS instead is a client-side decision (#596); this
        # persona reaching the proxy means that routing didn't happen.
        raise HTTPException(
            status_code=400,
            detail=f"persona_id {persona_id!r} orchestrates and cannot be forwarded to the hermes backend",
        )

    # Spoken-style rules apply only on voice turns, matching the exact gate
    # ask_stream() uses in api/routes/chat.py: `modality == "voice" and
    # request.persona_id`. Gating on the *raw* parsed.persona_id (not the
    # primary-defaulted `persona_id` above) matters: a voice turn that omits
    # persona_id entirely gets no voice rules natively, even though primary's
    # own persona file could define some — mirror that rather than inventing
    # a rule for the omitted-persona case.
    modality = "voice" if (parsed.modality or "").strip().lower() == "voice" else "text"
    voice_rules = (
        list(settings.persona_voice(persona_id)) if modality == "voice" and parsed.persona_id else []
    )

    # No fallback: list_http_personas() draws "primary" plus every entry in
    # the same settings.telegram_bots registry resolve_persona() just matched
    # persona_id against above, so a lookup miss here can't happen for an id
    # that already passed validation.
    label = next(p.label for p in settings.list_http_personas() if p.id == persona_id)

    data["lifeos_context"] = {
        "schema_version": 1,
        "modality": modality,
        "persona": {
            "id": persona_id,
            "label": label,
            "preamble": preamble,
            "voice_rules": voice_rules,
            # Derived, not hardcoded: the guard above already rejects an
            # orchestrating persona with a 400 before this point, so this is
            # always False in practice today. But the pinned contract tells
            # Hermes to fail loudly if it ever sees `true` here — a hardcoded
            # False would lie to that check if the guard above ever moved,
            # weakened, or got reordered. Deriving costs nothing and keeps
            # this field honest regardless.
            "orchestrates": settings.persona_orchestrates(persona_id),
        },
        # A sibling of `persona`, never merged into it (#591) — `persona` is
        # stable across a conversation and cacheable; `turn` changes every
        # turn. Built by the same function the turn-context endpoint uses, so
        # the two can't drift apart. Note: `personal_context` here resolves
        # from `persona_id` alone, unlike the native path's Telegram-preamble
        # reverse lookup above — Hermes turns always carry a persona_id (or
        # default to "primary"), so that reverse lookup doesn't apply here.
        "turn": build_turn_context(persona_id),
    }
    return json.dumps(data).encode("utf-8")


router = make_backend_router(
    prefix="/api/hermes",
    tag="hermes",
    backend_label="hermes",
    url_attr="hermes_backend_url",
    token_attr="hermes_backend_token",
    client_factory=lambda: _client(),
    transform_body=_build_envelope,
)
