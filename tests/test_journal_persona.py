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
- the create-then-append mechanism the persona is instructed to use actually
  produces a well-formed day file when driven against the real
  `/api/vault/write` route (same fixture pattern as test_vault_write_route.py);
- the reserved-path guard (tested directly in test_vault_write_route.py) is
  what makes "never targets Personal/Journal/" an enforced fact rather than a
  prompt suggestion.
"""
from __future__ import annotations

import importlib
import json
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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

    def test_omitted_without_token_present_with_token(self, tmp_path, monkeypatch):
        reg = tmp_path / "bots.json"
        reg.write_text(json.dumps([
            {"name": "journal", "token_env": "TG_JOURNAL", "persona_file": str(_PERSONA_PATH)},
        ]))
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
        monkeypatch.delenv("TG_JOURNAL", raising=False)
        from config.settings import settings
        assert "journal" not in [p.id for p in settings.list_http_personas()]

        monkeypatch.setenv("TG_JOURNAL", "tok")
        assert "journal" in [p.id for p in settings.list_http_personas()]

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

    def test_states_create_then_append_mechanism(self):
        text = _PERSONA_PATH.read_text()
        assert 'mode="create"' in text
        assert 'mode="append"' in text

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
# The create-then-append mechanism, exercised against the real vault_write
# route (same fixture pattern as test_vault_write_route.py).
# ---------------------------------------------------------------------------

@pytest.fixture
def app_and_vault(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="lifeos-journal-test-")
    monkeypatch.setenv("LIFEOS_VAULT_PATH", tmpdir)
    import config.settings
    importlib.reload(config.settings)
    from api.routes import vault
    importlib.reload(vault)
    app = FastAPI()
    app.include_router(vault.router)
    yield TestClient(app), Path(tmpdir)
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


def _day_file_header(date: str) -> str:
    return f"---\ntype: log\ndate: {date}\n---\n"


class TestCaptureMechanism:
    def test_first_fragment_of_day_creates_file_with_frontmatter(self, app_and_vault):
        c, vault = app_and_vault
        path = "Personal/Log/2026-08-23.md"
        content = _day_file_header("2026-08-23") + "- 09:14 · idea about the deploy gate #eng\n"
        r = c.post("/api/vault/write", json={"path": path, "content": content, "mode": "create"})
        assert r.status_code == 200, r.text
        assert r.json()["created"] is True

        written = (vault / "Personal" / "Log" / "2026-08-23.md").read_text()
        assert written.startswith("---\ntype: log\ndate: 2026-08-23\n---\n")
        assert "- 09:14 · idea about the deploy gate #eng" in written

    def test_second_fragment_falls_back_to_append_after_create_conflicts(self, app_and_vault):
        c, vault = app_and_vault
        path = "Personal/Log/2026-08-23.md"
        first = _day_file_header("2026-08-23") + "- 09:14 · idea about the deploy gate #eng\n"
        c.post("/api/vault/write", json={"path": path, "content": first, "mode": "create"})

        # Second fragment: create conflicts (file exists) -> fall back to append.
        second_bullet = '- 14:37 · that book title — "Seeing Like a State"\n'
        create_attempt = c.post("/api/vault/write", json={"path": path, "content": second_bullet, "mode": "create"})
        assert create_attempt.status_code == 409

        append = c.post("/api/vault/write", json={"path": path, "content": second_bullet, "mode": "append"})
        assert append.status_code == 200, append.text

        written = (vault / "Personal" / "Log" / "2026-08-23.md").read_text()
        # Frontmatter written exactly once, both bullets present, in order.
        assert written.count("type: log") == 1
        lines = [line for line in written.splitlines() if line.startswith("- ")]
        assert lines == [
            "- 09:14 · idea about the deploy gate #eng",
            '- 14:37 · that book title — "Seeing Like a State"',
        ]

    def test_capture_never_lands_in_reserved_journal_dir(self, app_and_vault):
        c, vault = app_and_vault
        r = c.post("/api/vault/write", json={
            "path": "Personal/Journal/2026-08-23.md",
            "content": _day_file_header("2026-08-23") + "- 09:14 · should never land here\n",
            "mode": "create",
        })
        assert r.status_code == 400
        assert not (vault / "Personal" / "Journal").exists()
