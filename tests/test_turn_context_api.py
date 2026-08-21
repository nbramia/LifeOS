"""Tests for GET /api/chat/turn-context (#591).

Covers the endpoint's literal response shape, persona-scoped
`personal_context`, the unknown-persona 400, and the empty-tags degradation
when the task manager is unreachable. Mirrors the registry-fixture pattern
in tests/test_persona_api.py.
"""
import json

import pytest
from fastapi.testclient import TestClient

from api.main import app
from config.settings import settings

pytestmark = pytest.mark.unit

_TURN_KEYS = {
    "current_datetime", "current_datetime_iso", "timezone",
    "time_resolution_instruction", "personal_context",
    "existing_tags", "tags_instruction",
    "session_cost_usd", "session_turn_count",
    "session_input_tokens", "session_output_tokens",
    "session_cost_is_lower_bound",
}


@pytest.fixture
def client():
    return TestClient(app)


def _registry(tmp_path, entries):
    reg = tmp_path / "bots.json"
    reg.write_text(json.dumps(entries))
    return reg


def test_shape_and_literal_keys(client):
    resp = client.get("/api/chat/turn-context")
    assert resp.status_code == 200
    body = resp.json()
    # Exact key set, not a subset check — matches the literal contract pinned
    # on #590 for lifeos_context.turn.
    assert set(body.keys()) == _TURN_KEYS
    assert isinstance(body["current_datetime"], str) and body["current_datetime"]
    assert isinstance(body["current_datetime_iso"], str) and body["current_datetime_iso"]
    assert body["timezone"] == settings.timezone
    assert isinstance(body["time_resolution_instruction"], str) and body["time_resolution_instruction"]
    assert isinstance(body["personal_context"], str)
    assert isinstance(body["existing_tags"], list)
    assert isinstance(body["tags_instruction"], str) and body["tags_instruction"]
    # No conversation_id given -- present and zero, not omitted or an error.
    assert body["session_cost_usd"] == 0.0
    assert body["session_turn_count"] == 0
    assert body["session_input_tokens"] == 0
    assert body["session_output_tokens"] == 0
    assert body["session_cost_is_lower_bound"] is False


def test_defaults_to_primary_persona(client):
    resp = client.get("/api/chat/turn-context")
    assert resp.status_code == 200
    # Same personal_context primary would get natively (empty — not therapist).
    assert resp.json()["personal_context"] == ""


def test_unknown_persona_id_returns_400(client):
    resp = client.get("/api/chat/turn-context", params={"persona_id": "ghost"})
    assert resp.status_code == 400
    assert "ghost" in resp.json()["detail"]


def _register_therapist(tmp_path, monkeypatch):
    """Give "therapist" a synthetic, self-contained registry entry so these
    tests don't depend on this machine's real config/telegram_bots.json
    entry plus a real TELEGRAM_THERAPIST_BOT_TOKEN happening to be set in
    the environment -- settings.telegram_bots() drops any entry whose token
    env var is unset, so "therapist" silently isn't a recognized persona at
    all without this (see #598: relying on ambient real config for a
    persona to resolve is exactly the kind of test-isolation gap that issue
    is about, even though this particular resolution isn't cached).
    """
    reg = _registry(tmp_path, [{"name": "therapist", "token_env": "TG_THERAPIST_TEST_TOKEN"}])
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_THERAPIST_TEST_TOKEN", "tok")


def test_personal_context_populated_for_therapist(client, tmp_path, monkeypatch):
    _register_therapist(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "partner_name", "Sam")
    monkeypatch.setattr(settings, "therapist_patterns", "Dr. A")
    resp = client.get("/api/chat/turn-context", params={"persona_id": "therapist"})
    assert resp.status_code == 200
    assert "Sam" in resp.json()["personal_context"]


def test_personal_context_empty_for_other_personas(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "partner_name", "Sam")
    monkeypatch.setattr(settings, "therapist_patterns", "Dr. A")
    persona_file = tmp_path / "fitness.md"
    persona_file.write_text("FIT PERSONA")
    reg = _registry(tmp_path, [
        {"name": "fitness", "token_env": "TG_FIT", "persona_file": str(persona_file)},
    ])
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_FIT", "tok")

    assert client.get(
        "/api/chat/turn-context", params={"persona_id": "primary"}
    ).json()["personal_context"] == ""
    assert client.get(
        "/api/chat/turn-context", params={"persona_id": "fitness"}
    ).json()["personal_context"] == ""


def test_personal_context_empty_when_config_unset(client, tmp_path, monkeypatch):
    _register_therapist(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "partner_name", "Partner")  # the placeholder default
    monkeypatch.setattr(settings, "therapist_patterns", "")
    resp = client.get("/api/chat/turn-context", params={"persona_id": "therapist"})
    assert resp.status_code == 200
    assert resp.json()["personal_context"] == ""


def test_empty_tags_when_task_manager_unreachable(client, monkeypatch):
    import api.services.task_manager as tm_mod

    def boom():
        raise RuntimeError("task manager unavailable")

    monkeypatch.setattr(tm_mod, "get_task_manager", boom)
    resp = client.get("/api/chat/turn-context")
    assert resp.status_code == 200  # a degraded case, not an error
    assert resp.json()["existing_tags"] == []


def test_existing_tags_with_counts(client, tmp_path, monkeypatch):
    from api.services.task_manager import TaskManager
    import api.services.task_manager as tm_mod

    manager = TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "task_index.json",
    )
    manager.create("a", tags=["work", "urgent"])
    manager.create("b", tags=["work"])
    monkeypatch.setattr(tm_mod, "get_task_manager", lambda: manager)

    resp = client.get("/api/chat/turn-context")
    assert resp.status_code == 200
    tags = {(t["tag"], t["count"]) for t in resp.json()["existing_tags"]}
    assert ("work", 2) in tags
    assert ("urgent", 1) in tags


def test_modality_accepted_but_does_not_change_response(client):
    """`modality` is accepted for shape symmetry with /api/ask/stream, but no
    field in `turn` varies with it (voice-specific material lives in
    `persona`, not `turn` — see the #590 pinned schema)."""
    text_body = client.get("/api/chat/turn-context", params={"modality": "text"}).json()
    voice_body = client.get("/api/chat/turn-context", params={"modality": "voice"}).json()
    # current_datetime(_iso) may tick a fraction of a second between calls —
    # compare everything else exactly.
    for key in _TURN_KEYS - {"current_datetime", "current_datetime_iso"}:
        assert text_body[key] == voice_body[key]


def test_read_only_does_not_mutate_tags(client, tmp_path, monkeypatch):
    """Calling the endpoint must never create, mutate, or persist anything —
    two calls in a row see the identical tag list."""
    from api.services.task_manager import TaskManager
    import api.services.task_manager as tm_mod

    manager = TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "task_index.json",
    )
    manager.create("a", tags=["work"])
    monkeypatch.setattr(tm_mod, "get_task_manager", lambda: manager)

    first = client.get("/api/chat/turn-context").json()["existing_tags"]
    second = client.get("/api/chat/turn-context").json()["existing_tags"]
    assert first == second == [{"tag": "work", "count": 1}]


# ---------------------------------------------------------------------------
# Session-to-date cost (#610) — `conversation_id` scopes `session_cost_usd`
# and friends to one conversation's already-recorded usage.
# ---------------------------------------------------------------------------

def test_session_cost_sums_prior_turns_for_the_conversation_id(client):
    from api.services.usage_store import get_usage_store

    store = get_usage_store()  # per-test isolated singleton (conftest)
    store.record_usage(
        model="claude-haiku-4-5", input_tokens=100, output_tokens=50,
        cost_usd=0.002, conversation_id="conv-x",
    )
    store.record_usage(
        model="claude-haiku-4-5", input_tokens=50, output_tokens=25,
        cost_usd=0.001, conversation_id="conv-x",
    )
    # A different conversation's usage must never leak into this sum.
    store.record_usage(
        model="claude-haiku-4-5", input_tokens=999, output_tokens=999,
        cost_usd=9.99, conversation_id="conv-y",
    )

    resp = client.get("/api/chat/turn-context", params={"conversation_id": "conv-x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_cost_usd"] == pytest.approx(0.003)
    assert body["session_input_tokens"] == 150
    assert body["session_output_tokens"] == 75
    assert body["session_turn_count"] == 2
    assert body["session_cost_is_lower_bound"] is False


def test_session_cost_unknown_conversation_id_is_zero_not_an_error(client):
    resp = client.get("/api/chat/turn-context", params={"conversation_id": "never-seen"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_cost_usd"] == 0.0
    assert body["session_turn_count"] == 0
    assert body["session_cost_is_lower_bound"] is False


def test_session_cost_zero_cost_turn_still_reports_a_truthful_sum(client):
    """A conversation containing a turn recorded with cost_usd=0.0 and
    unpriced=False (genuinely free) must still report a truthful sum and
    turn count for the whole conversation, not error or silently drop it
    -- and must not be flagged as a lower bound."""
    from api.services.usage_store import get_usage_store

    store = get_usage_store()
    store.record_usage(
        model="claude-haiku-4-5", input_tokens=100, output_tokens=50,
        cost_usd=0.002, conversation_id="conv-mixed",
    )
    store.record_usage(
        model="some-model", input_tokens=10, output_tokens=10,
        cost_usd=0.0, conversation_id="conv-mixed",
    )

    body = client.get("/api/chat/turn-context", params={"conversation_id": "conv-mixed"}).json()

    assert body["session_cost_usd"] == pytest.approx(0.002)
    assert body["session_input_tokens"] == 110
    assert body["session_output_tokens"] == 60
    assert body["session_turn_count"] == 2
    assert body["session_cost_is_lower_bound"] is False


def test_session_cost_unpriced_turn_marks_the_response_as_a_lower_bound(client):
    """#613: a conversation containing a turn recorded `unpriced=True`
    (its provider reported no cost) must surface `session_cost_is_lower_
    bound=True` in the standalone endpoint's response too, since it shares
    `build_turn_context()` with the Hermes envelope."""
    from api.services.usage_store import get_usage_store

    store = get_usage_store()
    store.record_usage(
        model="claude-haiku-4-5", input_tokens=100, output_tokens=50,
        cost_usd=0.002, conversation_id="conv-unpriced", unpriced=False,
    )
    store.record_usage(
        model="some-unrecognized-model", input_tokens=10, output_tokens=10,
        cost_usd=0.0, conversation_id="conv-unpriced", unpriced=True,
    )

    body = client.get("/api/chat/turn-context", params={"conversation_id": "conv-unpriced"}).json()

    assert body["session_cost_usd"] == pytest.approx(0.002)
    assert body["session_cost_is_lower_bound"] is True
