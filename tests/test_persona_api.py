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
    def test_primary_resolves_to_primary_md(self):
        # #390 P2: primary's personality now lives in config/personas/primary.md.
        from config.settings import settings
        pre = settings.resolve_persona("primary")
        assert pre and "general-purpose" in pre  # body loaded (no longer empty)
        assert not pre.lstrip().startswith("---")  # frontmatter stripped
        # The primary Telegram bot draws from the same file (single source).
        assert settings.telegram_primary_bot.persona == pre

    def test_primary_personality_moved_out_of_static_prompt(self):
        # The proactivity/tone now lives in primary.md, not the shared static prompt.
        import api.services.agent_system_prompt as asp
        from config.settings import _load_primary_persona
        body = _load_primary_persona()[0]
        assert "obvious next action" in body
        assert "obvious next action" not in asp._STATIC_PROMPT

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

    def test_malformed_frontmatter_falls_back_to_raw_body(self):
        # Invalid YAML must not raise (would 500 every persona request) — degrade gracefully.
        from config.settings import _parse_persona
        body, voice, model = _parse_persona("---\nid: x\nvoice: [unterminated\n---\n\nBODY", "x")
        assert "BODY" in body
        assert voice == ()
        assert model == ""

    def test_non_list_voice_is_ignored(self):
        from config.settings import _parse_persona
        body, voice, model = _parse_persona("---\nid: x\nvoice: just a scalar\n---\n\nB", "x")
        assert voice == ()
        assert body == "B"

    def test_id_mismatch_warns(self, caplog):
        import logging
        from config.settings import _parse_persona
        with caplog.at_level(logging.WARNING):
            _parse_persona("---\nid: wrong\n---\n\nB", "right")
        assert any("does not match" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Voice-awareness (#390 Phase 3)
# ---------------------------------------------------------------------------

class TestVoiceAwareness:
    def test_build_system_prompt_appends_spoken_block(self):
        from api.services.agent_system_prompt import build_system_prompt
        blocks = build_system_prompt(persona="P", voice_rules=("no markdown", "be brief"))
        joined = "\n".join(b["text"] for b in blocks)
        assert "Spoken response" in joined and "no markdown" in joined and "be brief" in joined

    def test_build_system_prompt_no_spoken_block_for_text(self):
        from api.services.agent_system_prompt import build_system_prompt
        blocks = build_system_prompt(persona="P")
        assert not any("Spoken response" in b["text"] for b in blocks)

    def test_persona_voice_returns_rules(self, tmp_path, monkeypatch):
        pf = tmp_path / "fitness.md"
        pf.write_text("---\nid: fitness\nvoice:\n  - terse\n  - no emoji\n---\n\nB")
        reg = _registry(tmp_path, [{"name": "fitness", "token_env": "TG_F", "persona_file": str(pf)}])
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_F", "tok")
        from config.settings import settings
        assert settings.persona_voice("fitness") == ("terse", "no emoji")
        assert settings.persona_voice("ghost") == ()

    def test_voice_modality_threads_voice_rules(self, client, monkeypatch):
        # A modality="voice" turn appends the persona's voice rules; text does not.
        from types import SimpleNamespace
        import api.services.agent_loop as agent_loop_mod
        captured: dict = {}

        async def fake_loop(**kwargs):
            captured.clear()
            captured.update(kwargs)
            yield {"type": "result", "result": SimpleNamespace(
                total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
                model="m", tool_calls_log=[], full_text="ok")}

        async def fake_classify(*a, **k):
            return None

        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
        monkeypatch.setattr("api.routes.chat.classify_action_intent", fake_classify)

        r = client.post("/api/ask/stream", json={"question": "hi", "persona_id": "primary", "modality": "voice"})
        assert r.status_code == 200
        assert captured.get("voice_rules")  # non-empty — primary.md carries voice rules

        client.post("/api/ask/stream", json={"question": "hi", "persona_id": "primary"})
        assert captured.get("voice_rules") == ()

    def test_voice_block_is_uncached(self):
        from api.services.agent_system_prompt import build_system_prompt
        blocks = build_system_prompt(persona="P", voice_rules=("brief",))
        spoken = next(b for b in blocks if "Spoken response" in b["text"])
        assert "cache_control" not in spoken  # uncached, like the persona block

    def test_raw_persona_path_gets_no_voice_rules(self, client, monkeypatch):
        # The raw `persona` path (no id) + modality=voice gets no voice rules,
        # rather than misapplying primary's (guard on persona_id).
        from types import SimpleNamespace
        import api.services.agent_loop as agent_loop_mod
        captured: dict = {}

        async def fake_loop(**kwargs):
            captured.clear()
            captured.update(kwargs)
            yield {"type": "result", "result": SimpleNamespace(
                total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
                model="m", tool_calls_log=[], full_text="ok")}

        async def fake_classify(*a, **k):
            return None

        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
        monkeypatch.setattr("api.routes.chat.classify_action_intent", fake_classify)
        r = client.post("/api/ask/stream", json={"question": "hi", "persona": "RAW", "modality": "voice"})
        assert r.status_code == 200
        assert captured.get("voice_rules") == ()


# ---------------------------------------------------------------------------
# Personal-context resolution (#390 Phase 4)
# ---------------------------------------------------------------------------

class TestPersonalContext:
    def test_personal_context_therapist_from_config(self, monkeypatch):
        from config.settings import settings
        monkeypatch.setattr(settings, "partner_name", "Sam")
        monkeypatch.setattr(settings, "therapist_patterns", "Dr. A|Dr. B")
        block = settings.personal_context("therapist")
        assert "Partner: Sam" in block and "Dr. A, Dr. B" in block
        assert settings.personal_context("primary") == ""   # scoped to therapist
        assert settings.personal_context("fitness") == ""

    def test_personal_context_empty_when_config_unset(self, monkeypatch):
        from config.settings import settings
        monkeypatch.setattr(settings, "partner_name", "Partner")  # the default → skipped
        monkeypatch.setattr(settings, "therapist_patterns", "")
        assert settings.personal_context("therapist") == ""

    def test_build_system_prompt_appends_personal_context(self):
        from api.services.agent_system_prompt import build_system_prompt
        blocks = build_system_prompt(persona="P", personal_context="## Your people\n\n- Partner: X")
        assert any("Your people" in b["text"] and "Partner: X" in b["text"] for b in blocks)

    def test_therapist_threading_both_paths(self, client, tmp_path, monkeypatch):
        from types import SimpleNamespace
        import api.services.agent_loop as agent_loop_mod
        from config.settings import settings
        pf = tmp_path / "therapist.md"
        pf.write_text("THERAPY PERSONA")
        reg = _registry(tmp_path, [{"name": "therapist", "token_env": "TG_TH", "persona_file": str(pf)}])
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_TH", "tok")
        monkeypatch.setattr(settings, "partner_name", "Sam")
        monkeypatch.setattr(settings, "therapist_patterns", "Dr. A")
        captured: dict = {}

        async def fake_loop(**kwargs):
            captured.clear()
            captured.update(kwargs)
            yield {"type": "result", "result": SimpleNamespace(
                total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
                model="m", tool_calls_log=[], full_text="ok")}

        async def fake_classify(*a, **k):
            return None

        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
        monkeypatch.setattr("api.routes.chat.classify_action_intent", fake_classify)

        # persona_id path (web/voice)
        client.post("/api/ask/stream", json={"question": "hi", "persona_id": "therapist"})
        assert "Sam" in (captured.get("personal_context") or "")
        # raw-persona path (Telegram) → reverse-lookup → same block
        client.post("/api/ask/stream", json={"question": "hi", "persona": "THERAPY PERSONA"})
        assert "Sam" in (captured.get("personal_context") or "")
        # a different persona gets none
        client.post("/api/ask/stream", json={"question": "hi", "persona_id": "primary"})
        assert captured.get("personal_context") == ""


# ---------------------------------------------------------------------------
# Web-spawn for orchestrating personas (#390 Phase 5)
# ---------------------------------------------------------------------------

class TestOrchestratingPersonaSpawn:
    def _doctor_registry(self, tmp_path, monkeypatch):
        pf = tmp_path / "doctor.md"
        pf.write_text("DOCTOR PIPELINE")
        reg = _registry(tmp_path, [
            {"name": "doctor", "token_env": "TG_D", "persona_file": str(pf), "orchestrates": True},
        ])
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_D", "tok")

    def test_persona_orchestrates(self, tmp_path, monkeypatch):
        self._doctor_registry(tmp_path, monkeypatch)
        from config.settings import settings
        assert settings.persona_orchestrates("doctor") is True
        assert settings.persona_orchestrates("primary") is False
        assert settings.persona_orchestrates("ghost") is False

    def test_doctor_persona_spawns_claude_code_not_inline(self, client, tmp_path, monkeypatch):
        from types import SimpleNamespace
        import api.services.agent_loop as agent_loop_mod
        import api.services.agent_worker.claude_code_spawn as ccs
        self._doctor_registry(tmp_path, monkeypatch)
        spawned: dict = {}

        def fake_spawn(store, prompt, **kw):
            spawned["prompt"] = prompt
            spawned["kw"] = kw
            return {"ok": True, "session_id": "sess_test12345abc"}

        loop_calls = {"n": 0}

        async def fake_loop(**kwargs):
            loop_calls["n"] += 1
            yield {"type": "result", "result": SimpleNamespace(
                total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
                model="m", tool_calls_log=[], full_text="x")}

        monkeypatch.setattr(ccs, "spawn_claude_code_session", fake_spawn)
        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
        monkeypatch.setattr("api.services.agent_worker.session_store.SessionStore", lambda *a, **k: object())

        r = client.post("/api/ask/stream", json={"question": "lifeos is broken", "persona_id": "doctor"})
        assert r.status_code == 200
        body = r.text
        assert "Claude Code session" in body and "sess_test12" in body  # ack streamed back
        assert "DOCTOR PIPELINE" in spawned["prompt"]      # the persona pipeline is the prompt
        assert "lifeos is broken" in spawned["prompt"]     # plus the user's message
        assert loop_calls["n"] == 0                         # did NOT fall through to the inline orchestrator


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
