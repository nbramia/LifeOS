"""
Tests for HTTP persona discovery and persona_id chat scoping (issue #351).

Covers the settings registry helpers (list_http_personas / resolve_persona),
the GET /api/personas discovery endpoint, persona_id resolution + 400 handling
on POST /api/ask/stream, and persona-scoped conversation storage/listing.
"""
import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app

pytestmark = pytest.mark.unit


@pytest.fixture
def client():
    return TestClient(app)


def _registry(tmp_path, entries):
    """Write a telegram_bots.json registry and point settings at it."""
    reg = tmp_path / "bots.json"
    reg.write_text(json.dumps(entries))
    return reg


# ---------------------------------------------------------------------------
# settings.list_http_personas
# ---------------------------------------------------------------------------

class TestListHttpPersonas:
    def test_primary_plus_configured_bots(self, tmp_path, monkeypatch):
        persona_file = tmp_path / "fitness.md"
        persona_file.write_text("FIT PERSONA")
        reg = _registry(tmp_path, [
            {"name": "fitness", "token_env": "TG_FIT", "persona_file": str(persona_file)},
        ])
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_FIT", "tok")
        from config.settings import settings

        personas = settings.list_http_personas()
        assert [p.id for p in personas] == ["primary", "fitness"]

        primary = personas[0]
        assert primary.label == "Primary"
        assert primary.capabilities == ["handoff", "agent"]

        fitness = personas[1]
        assert fitness.label == "Fitness"  # capitalized default
        assert fitness.capabilities == []  # specialized bots are pure chat

    def test_unset_token_bot_omitted(self, tmp_path, monkeypatch):
        reg = _registry(tmp_path, [
            {"name": "fitness", "token_env": "TG_FIT"},
            {"name": "therapist", "token_env": "TG_THER"},
        ])
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_FIT", "tok")
        monkeypatch.delenv("TG_THER", raising=False)
        from config.settings import settings

        assert [p.id for p in settings.list_http_personas()] == ["primary", "fitness"]

    def test_new_registry_entry_surfaces_without_code_change(self, tmp_path, monkeypatch):
        # Acceptance: adding a registry entry + env var surfaces a new persona.
        reg = _registry(tmp_path, [{"name": "doctor", "token_env": "TG_DOC"}])
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_DOC", "tok")
        from config.settings import settings

        ids = [p.id for p in settings.list_http_personas()]
        assert "doctor" in ids

    def test_custom_label_from_registry(self, tmp_path, monkeypatch):
        reg = _registry(tmp_path, [
            {"name": "doctor", "token_env": "TG_DOC", "label": "Dr. LifeOS"},
        ])
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_DOC", "tok")
        from config.settings import settings

        doctor = settings.list_http_personas()[1]
        assert doctor.label == "Dr. LifeOS"

    def test_orchestrating_bot_advertises_capabilities(self, tmp_path, monkeypatch):
        # An orchestrating bot (e.g. the doctor self-repair bot) drives Claude
        # Code sessions, so it advertises handoff/agent like the primary; a
        # pure-chat bot does not.
        reg = _registry(tmp_path, [
            {"name": "doctor", "token_env": "TG_DOC", "orchestrates": True},
            {"name": "fitness", "token_env": "TG_FIT"},
        ])
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_DOC", "tok")
        monkeypatch.setenv("TG_FIT", "tok")
        from config.settings import settings

        by_id = {p.id: p for p in settings.list_http_personas()}
        assert by_id["doctor"].capabilities == ["handoff", "agent"]
        assert by_id["fitness"].capabilities == []


# ---------------------------------------------------------------------------
# settings.resolve_persona
# ---------------------------------------------------------------------------

class TestResolvePersona:
    def test_primary_resolves_to_empty(self):
        from config.settings import settings
        assert settings.resolve_persona("primary") == ""

    def test_unknown_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", tmp_path / "none.json")
        from config.settings import settings
        assert settings.resolve_persona("ghost") is None

    def test_known_resolves_to_bot_preamble(self, tmp_path, monkeypatch):
        persona_file = tmp_path / "fitness.md"
        persona_file.write_text("FIT PERSONA")
        reg = _registry(tmp_path, [
            {"name": "fitness", "token_env": "TG_FIT", "persona_file": str(persona_file)},
        ])
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_FIT", "tok")
        from config.settings import settings

        # Same preamble the fitness Telegram bot uses (single registry source).
        assert settings.resolve_persona("fitness") == "FIT PERSONA"
        assert settings.resolve_persona("fitness") == settings.telegram_bots[0].persona


# ---------------------------------------------------------------------------
# Persona frontmatter loader (#390 Phase 1)
# ---------------------------------------------------------------------------

class TestPersonaFrontmatter:
    def test_parse_strips_frontmatter_keeps_body_and_braces(self):
        from config.settings import _parse_persona
        text = (
            "---\n"
            "id: fitness\n"
            "model: opus\n"
            "voice:\n"
            "  - lead with the number\n"
            "  - no markdown\n"
            "---\n\n"
            'You are the fitness bot. Log `bench 135x8` as {exercise: "bench"}.\n'
        )
        body, voice, model = _parse_persona(text, "fitness")
        assert body.startswith("You are the fitness bot.")
        assert "---" not in body and "id: fitness" not in body  # frontmatter stripped
        assert '{exercise: "bench"}' in body  # literal braces survive (no str.format)
        assert voice == ("lead with the number", "no markdown")
        assert model == "opus"

    def test_parse_no_frontmatter_passthrough(self):
        from config.settings import _parse_persona
        body, voice, model = _parse_persona("Just a body with a {literal} brace.", "x")
        assert body == "Just a body with a {literal} brace."
        assert voice == ()
        assert model == ""

    def test_resolve_persona_returns_body_only(self, tmp_path, monkeypatch):
        pf = tmp_path / "therapist.md"
        pf.write_text("---\nid: therapist\nvoice:\n  - calm\n---\n\nADVICE BODY.")
        reg = _registry(tmp_path, [
            {"name": "therapist", "token_env": "TG_TH", "persona_file": str(pf)},
        ])
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_TH", "tok")
        from config.settings import settings
        assert settings.resolve_persona("therapist") == "ADVICE BODY."  # no YAML leak
        bot = settings.telegram_bots[0]
        assert bot.voice == ("calm",)
        assert bot.model == ""

    def test_real_persona_files_parse_clean(self):
        from pathlib import Path
        from config.settings import _parse_persona
        files = [f for f in Path("config/personas").glob("*.md") if f.name != "README.md"]
        assert files, "no persona files found"
        for f in files:
            body, _voice, _model = _parse_persona(f.read_text(), f.stem)
            assert body, f"{f.name}: empty body"
            assert not body.lstrip().startswith(("---", "id:")), f"{f.name}: frontmatter leaked into body"


# ---------------------------------------------------------------------------
# GET /api/personas
# ---------------------------------------------------------------------------

class TestPersonasEndpoint:
    def test_discovery_endpoint(self, client, tmp_path, monkeypatch):
        persona_file = tmp_path / "fitness.md"
        persona_file.write_text("FIT PERSONA")
        reg = _registry(tmp_path, [
            {"name": "fitness", "token_env": "TG_FIT", "persona_file": str(persona_file)},
        ])
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_FIT", "tok")

        resp = client.get("/api/personas")
        assert resp.status_code == 200
        data = resp.json()
        assert [p["id"] for p in data["personas"]] == ["primary", "fitness"]
        assert data["personas"][0]["capabilities"] == ["handoff", "agent"]
        assert data["personas"][1]["capabilities"] == []


# ---------------------------------------------------------------------------
# GET /api/chat/config
# ---------------------------------------------------------------------------

class TestChatConfigEndpoint:
    def test_default_voice_reflects_setting(self, client, monkeypatch):
        monkeypatch.setattr("api.routes.chat.settings.chat_default_voice", False, raising=False)
        assert client.get("/api/chat/config").json() == {"default_voice": False}
        monkeypatch.setattr("api.routes.chat.settings.chat_default_voice", True, raising=False)
        assert client.get("/api/chat/config").json() == {"default_voice": True}


# ---------------------------------------------------------------------------
# persona_id resolution on POST /api/ask/stream
# ---------------------------------------------------------------------------

class TestAskStreamPersonaId:
    def test_unknown_persona_id_returns_400(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", tmp_path / "none.json")
        resp = client.post("/api/ask/stream", json={"question": "hi", "persona_id": "ghost"})
        assert resp.status_code == 400
        assert "ghost" in resp.json()["detail"]

    def test_persona_and_persona_id_conflict_returns_400(self, client):
        resp = client.post(
            "/api/ask/stream",
            json={"question": "hi", "persona": "X", "persona_id": "primary"},
        )
        assert resp.status_code == 400

    def test_persona_id_applies_resolved_preamble(self, client, tmp_path, monkeypatch):
        # End-to-end: persona_id="fitness" threads the fitness preamble into the
        # agent loop, identical to what the fitness Telegram bot sends.
        persona_file = tmp_path / "fitness.md"
        persona_file.write_text("FIT PERSONA")
        reg = _registry(tmp_path, [
            {"name": "fitness", "token_env": "TG_FIT", "persona_file": str(persona_file)},
        ])
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_FIT", "tok")

        captured = {}

        async def fake_loop(**kwargs):
            captured.update(kwargs)
            yield {"type": "result", "result": SimpleNamespace(
                total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
                model="x", tool_calls_log=[], full_text="ok",
            )}

        def fake_resolve(history, question, model, escalation_model):
            return model, False

        mock_store = MagicMock()
        mock_store.create_conversation.return_value = SimpleNamespace(id="c1")
        mock_store.get_messages.return_value = []

        with patch("api.routes.chat.get_store", return_value=mock_store), \
             patch("api.routes.chat.classify_action_intent", return_value=None), \
             patch("api.services.agent_loop.run_agent_loop", fake_loop), \
             patch("api.services.agent_loop.resolve_orchestrator_model", fake_resolve):
            resp = client.post(
                "/api/ask/stream",
                json={"question": "log my workout", "persona_id": "fitness"},
            )
            # Drain the SSE stream so generate() runs to completion.
            _ = resp.text

        assert resp.status_code == 200
        assert captured.get("persona") == "FIT PERSONA"
        # New conversation tagged with the selected persona.
        assert mock_store.create_conversation.call_args.kwargs.get("persona_id") == "fitness"


# ---------------------------------------------------------------------------
# persona-scoped conversation storage + listing
# ---------------------------------------------------------------------------

class TestConversationPersonaScoping:
    @pytest.fixture
    def store(self):
        from api.services.conversation_store import ConversationStore
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            yield ConversationStore(db_path=path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_create_defaults_to_primary(self, store):
        conv = store.create_conversation(title="t")
        assert conv.persona_id == "primary"
        assert store.get_conversation(conv.id).persona_id == "primary"

    def test_create_with_persona_id(self, store):
        conv = store.create_conversation(title="t", persona_id="fitness")
        assert conv.persona_id == "fitness"
        assert store.get_conversation(conv.id).persona_id == "fitness"

    def test_list_filters_by_persona(self, store):
        store.create_conversation(title="web", persona_id="primary")
        store.create_conversation(title="fit", persona_id="fitness")
        store.create_conversation(title="fit2", persona_id="fitness")

        primary = store.list_conversations(persona_id="primary")
        fitness = store.list_conversations(persona_id="fitness")
        assert {c.title for c in primary} == {"web"}
        assert {c.title for c in fitness} == {"fit", "fit2"}

    def test_list_without_filter_returns_all(self, store):
        store.create_conversation(title="web", persona_id="primary")
        store.create_conversation(title="fit", persona_id="fitness")
        assert len(store.list_conversations()) == 2

    def test_migration_backfills_existing_rows(self):
        # A pre-#351 conversations table (no persona_id column) must migrate and
        # backfill existing rows to 'primary'.
        import sqlite3
        from api.services.conversation_store import ConversationStore

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
                "created_at TIMESTAMP, updated_at TIMESTAMP)"
            )
            conn.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) "
                "VALUES ('old', 'legacy', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            conn.commit()
            conn.close()

            store = ConversationStore(db_path=path)  # triggers migration
            conv = store.get_conversation("old")
            assert conv is not None
            assert conv.persona_id == "primary"
        finally:
            if os.path.exists(path):
                os.unlink(path)


# ---------------------------------------------------------------------------
# GET /api/conversations?persona_id=<id>
# ---------------------------------------------------------------------------

class TestConversationsListPersonaParam:
    def test_default_param_is_primary(self, client):
        mock_store = MagicMock()
        mock_store.list_conversations.return_value = []
        with patch("api.routes.conversations.get_store", return_value=mock_store):
            resp = client.get("/api/conversations")
        assert resp.status_code == 200
        assert mock_store.list_conversations.call_args.kwargs.get("persona_id") == "primary"

    def test_explicit_persona_param_forwarded(self, client):
        mock_store = MagicMock()
        mock_store.list_conversations.return_value = []
        with patch("api.routes.conversations.get_store", return_value=mock_store):
            resp = client.get("/api/conversations?persona_id=fitness")
        assert resp.status_code == 200
        assert mock_store.list_conversations.call_args.kwargs.get("persona_id") == "fitness"
