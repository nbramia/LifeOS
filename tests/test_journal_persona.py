"""Tests for the `journal` persona (#659) — disjointed-fragment capture into
`Personal/Log/YYYY-MM-DD.md`, distinct from the generated `Personal/Journal/`
daily journal that `journal_trends.py` analyzes.

The persona itself is prose read by the orchestrating LLM (no dedicated code
path decides what to log or when to create a task), so its actual runtime
behavior isn't unit-testable without invoking a model. What *is* testable
without one:

- the registry wiring (the bot appears/disappears with its token, like every
  other specialized bot — mirrors test_persona_api.py / test_doctor_bot.py);
- the persona file itself states the behavior the issue specifies (mirrors
  test_doctor_bot.py's needle-based check on doctor.md);
- that capture lands where the persona says it lands, driven through the real
  capture path (#674 moved capture out of the persona's prompt and into
  `api/services/journal_capture.py`, because the tool it used to be told to
  call — `lifeos_vault_write` — is MCP-only and absent from the native agentic
  loop, so nothing was ever written; the mechanism's full behavior, including
  end-to-end captures through `/chat` and the ring-ingest endpoint, lives in
  tests/test_journal_capture.py);
- the reserved-path guard (tested directly in test_vault_write_route.py) is
  what makes "never targets Personal/Journal/" an enforced fact rather than a
  prompt suggestion.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import vault as vault_route

pytestmark = pytest.mark.unit

_PERSONA_PATH = Path("config/personas/journal.md")
_REGISTRY_PATH = Path("config/telegram_bots.json")


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_journal_entry_present_in_shipped_registry(self):
        entries = json.loads(_REGISTRY_PATH.read_text())
        journal = next((e for e in entries if e["name"] == "journal"), None)
        assert journal is not None, "no 'journal' entry in config/telegram_bots.json"
        assert journal["persona_file"] == "config/personas/journal.md"
        assert journal["token_env"] == "TELEGRAM_JOURNAL_BOT_TOKEN"
        # Pure chat, like fitness/therapist/finance — not an orchestrator.
        assert not journal.get("orchestrates", False)

    def test_listed_for_http_with_or_without_token(self, tmp_path, monkeypatch):
        """Journal is reachable in /chat whether or not a Telegram bot exists.

        Only the Telegram listener needs a token; requiring one to see the
        persona in a browser meant creating a bot you never intend to message.
        """
        reg = tmp_path / "bots.json"
        reg.write_text(json.dumps([
            {"name": "journal", "token_env": "TG_JOURNAL", "persona_file": str(_PERSONA_PATH)},
        ]))
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.delenv("TG_JOURNAL", raising=False)
        from config.settings import settings
        assert "journal" in [p.id for p in settings.list_http_personas()]
        assert [b.name for b in settings.telegram_bots] == []  # no token -> no listener

        monkeypatch.setenv("TG_JOURNAL", "tok")
        assert "journal" in [p.id for p in settings.list_http_personas()]
        assert [b.name for b in settings.telegram_bots] == ["journal"]

    def test_pure_chat_advertises_no_capabilities(self, tmp_path, monkeypatch):
        reg = tmp_path / "bots.json"
        reg.write_text(json.dumps([
            {"name": "journal", "token_env": "TG_JOURNAL", "persona_file": str(_PERSONA_PATH)},
        ]))
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.setenv("TG_JOURNAL", "tok")
        from config.settings import settings
        journal = next(p for p in settings.list_http_personas() if p.id == "journal")
        assert journal.capabilities == []
        assert journal.orchestrates is False


# ---------------------------------------------------------------------------
# Persona content — mirrors test_doctor_bot.py's needle check on doctor.md
# ---------------------------------------------------------------------------

class TestPersonaContent:
    def test_id_matches_registry_name(self):
        import frontmatter
        post = frontmatter.loads(_PERSONA_PATH.read_text())
        assert post.metadata.get("id") == "journal"

    def test_states_capture_target_and_bullet_shape(self):
        text = _PERSONA_PATH.read_text()
        assert "Personal/Log/YYYY-MM-DD.md" in text
        assert "HH:MM" in text

    def test_never_writes_to_reserved_journal_dir(self):
        text = _PERSONA_PATH.read_text()
        assert "Personal/Journal/" in text
        low = text.lower()
        assert "never write" in low or "off-limits" in low

    def test_states_frontmatter_requirement(self):
        text = _PERSONA_PATH.read_text()
        assert "type: log" in text
        assert "date:" in text

    def test_does_not_instruct_a_tool_the_agentic_loop_lacks(self):
        """#674: the persona used to tell the model to append the bullet via
        `lifeos_vault_write` — MCP-only, absent from the native loop's
        TOOL_DEFINITIONS. The model could not call it, wrote the bullet as
        prose, and every fragment was lost. Capture is code now
        (api/services/journal_capture.py); the persona must not claim
        otherwise."""
        text = _PERSONA_PATH.read_text()
        assert "lifeos_vault_write" not in text
        assert 'mode="create"' not in text
        assert 'mode="append"' not in text

    def test_states_capture_is_automatic_and_not_the_models_job(self):
        text = _PERSONA_PATH.read_text()
        low = text.lower()
        assert "do not try to write the log yourself" in low
        assert "before your turn starts" in low

    def test_states_extraction_tools_and_all_three_cases(self):
        text = _PERSONA_PATH.read_text()
        assert "lifeos_task_create" in text
        assert "lifeos_schedule_create" in text
        # The three worked examples from the issue, verbatim enough to prove
        # each behavior (silent schedule / one question / log-only) is spelled out.
        assert "call mum Thursday 3pm" in text
        assert "I should really call mum" in text
        assert "mum's birthday soon" in text
        assert "Want a task for that?" in text

    def test_no_real_personal_data(self):
        # Open-source rule: examples must be obviously synthetic, no real
        # names/paths/tokens (config/personas/README.md).
        text = _PERSONA_PATH.read_text()
        for leaked in ("nathanramia", "/home/", "TELEGRAM_JOURNAL_BOT_TOKEN="):
            assert leaked not in text


# ---------------------------------------------------------------------------
# Where capture actually lands, driven through the real capture path (#674).
# Mechanism detail lives in tests/test_journal_capture.py; this file only
# holds the persona-level invariants to its word.
# ---------------------------------------------------------------------------

@pytest.fixture
def vault(tmp_path, monkeypatch) -> Path:
    """A throwaway vault. Asserts the redirect actually took — a capture test
    that silently ran against the operator's real vault would be far worse
    than a failing one.

    Patches `vault_path` on the settings object each module actually holds,
    not just the current `config.settings.settings`: tests/test_vault_write_route.py
    reloads `config.settings`, after which a module that did
    `from config.settings import settings` at import time keeps the ORIGINAL
    instance — the one still pointing at the operator's real vault.
    """
    import api.services.journal_capture as journal_capture_mod
    import config.settings as settings_mod
    from api.routes import vault as vault_route_mod

    root = tmp_path / "vault"
    root.mkdir()
    seen = {}
    for obj in (settings_mod.settings, journal_capture_mod.settings, vault_route_mod.settings):
        seen[id(obj)] = obj
    for obj in seen.values():
        monkeypatch.setattr(obj, "vault_path", root)
    assert journal_capture_mod.settings.vault_path == root
    return root


class TestCaptureTarget:
    def test_fragment_lands_in_the_day_log_the_persona_names(self, vault):
        from datetime import datetime
        from api.services.journal_capture import capture_fragment

        capture_fragment("idea about the deploy gate #eng", now=datetime(2026, 8, 23, 9, 14))
        written = (vault / "Personal" / "Log" / "2026-08-23.md").read_text()
        assert written.startswith("---\ntype: log\ndate: 2026-08-23\n---\n")
        assert "- 09:14 · idea about the deploy gate #eng" in written

    def test_capture_never_lands_in_reserved_journal_dir(self, vault):
        from datetime import datetime
        from api.services.journal_capture import capture_fragment

        capture_fragment("should never land in the generated journal", now=datetime(2026, 8, 23, 9, 14))
        assert not (vault / "Personal" / "Journal").exists()

    def test_vault_write_route_still_reserves_personal_journal(self, vault, monkeypatch):
        # The prompt-level "never write here" is backed by a route-level guard;
        # #674 must not have weakened it. Since #769 the guard only applies
        # when the journal persona is enabled — enable it here (as this
        # install, running the journal persona, would have it) so this stays
        # a true regression guard rather than exercising the unenabled default.
        monkeypatch.setenv("TELEGRAM_JOURNAL_BOT_TOKEN", "test-token")
        app = FastAPI()
        app.include_router(vault_route.router)
        c = TestClient(app)
        r = c.post("/api/vault/write", json={
            "path": "Personal/Journal/2026-08-23.md",
            "content": "should never land here",
            "mode": "create",
        })
        assert r.status_code == 400
        assert not (vault / "Personal" / "Journal").exists()
