"""#674: journal capture actually writes the fragment.

The bug these cover: the `journal` persona was told to append the bullet
itself via `lifeos_vault_write`, a tool the native agentic loop does not have.
The model emitted the bullet as chat prose, the reply looked like a successful
capture, and nothing was written — while #659's and #660's tests passed,
because both mock the chat pipeline and assert it was *called*, never that a
fragment lands.

So these deliberately do the opposite: they assert on the **content of the
file on disk**, driving the real `chat.ask_stream()` (only the model itself is
faked) and, for the ring-ingest path, the real endpoint on top of it. A test
here fails if the write silently vanishes.

Never touches the real vault: every test runs against a `tmp_path` vault, and
`_vault` asserts the redirect took before yielding.
"""
from __future__ import annotations

import json
import re
import traceback
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.chat as chat
import api.routes.journal_ingest as journal_ingest
import api.routes.vault as vault_route
import api.services.journal_ingest_store as journal_ingest_store
from api.services.journal_capture import (
    JournalCaptureError,
    capture_fragment,
    log_path_for,
)
from api.services.journal_ingest_store import JournalIngestStore
from config.settings import settings

pytestmark = pytest.mark.unit

# Obviously synthetic fragments — nothing here resembles a real note.
_FRAGMENT = "the deploy gate should fail closed #eng"
_FRAGMENT_2 = 'that book title — "Seeing Like a State"'


def _today_log(vault: Path) -> Path:
    return vault / log_path_for(date.today())


# ---------------------------------------------------------------------------
# Fixtures
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


@pytest.fixture
def journal_persona(tmp_path, monkeypatch) -> str:
    """Register a `journal` bot the way test_persona_api.py does, so both
    persona paths into `ask_stream` resolve: `persona_id="journal"` (web/voice)
    and the raw-preamble reverse lookup (Telegram / ring ingest)."""
    reg = tmp_path / "bots.json"
    reg.write_text(json.dumps([{
        "name": "journal",
        "token_env": "TG_JOURNAL_TEST",
        "persona_file": "config/personas/journal.md",
    }]))
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", reg)
    monkeypatch.setenv("TG_JOURNAL_TEST", "tok")
    preamble = settings.resolve_persona("journal")
    assert preamble, "journal persona did not resolve"
    return preamble


@pytest.fixture
def fake_model(monkeypatch):
    """Replace the model, and only the model. Its reply is deliberately one
    that does NOT contain the fragment — if a test still finds the fragment on
    disk, code put it there."""
    import api.services.agent_loop as agent_loop_mod

    async def fake_loop(**kwargs):
        yield {"type": "text", "content": "Logged."}
        yield {"type": "result", "result": SimpleNamespace(
            total_input_tokens=1, total_output_tokens=1, total_cost_usd=0.0,
            model="fake", tool_calls_log=[], full_text="Logged.",
        )}

    monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)

    async def fake_classify(*a, **k):
        return None

    monkeypatch.setattr(chat, "classify_action_intent", fake_classify)


async def _run_turn(**request_kwargs) -> list[dict]:
    """Drive a real `chat.ask_stream()` to completion and return its SSE
    events. This is the whole pipeline minus the model."""
    response = await chat.ask_stream(chat.AskStreamRequest(**request_kwargs))
    events: list[dict] = []
    async for raw in response.body_iterator:
        text = raw.decode() if isinstance(raw, bytes) else raw
        for line in text.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


def _bullets(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- ")]


# ---------------------------------------------------------------------------
# The capture service itself
# ---------------------------------------------------------------------------

class TestCaptureService:
    def test_first_fragment_writes_frontmatter_and_bullet(self, vault):
        result = capture_fragment(_FRAGMENT, now=datetime(2026, 8, 23, 9, 14))
        assert result.path == "Personal/Log/2026-08-23.md"
        assert result.created is True

        written = (vault / "Personal" / "Log" / "2026-08-23.md").read_text()
        assert written == (
            "---\ntype: log\ndate: 2026-08-23\n---\n"
            f"- 09:14 · {_FRAGMENT}\n"
        )

    def test_later_fragments_append_below_a_single_header(self, vault):
        capture_fragment(_FRAGMENT, now=datetime(2026, 8, 23, 9, 14))
        second = capture_fragment(_FRAGMENT_2, now=datetime(2026, 8, 23, 14, 37))
        assert second.created is False

        written = (vault / "Personal" / "Log" / "2026-08-23.md").read_text()
        assert written.count("type: log") == 1
        assert written.count("---") == 2
        assert _bullets(written) == [
            f"- 09:14 · {_FRAGMENT}",
            f"- 14:37 · {_FRAGMENT_2}",
        ]

    def test_fragment_is_verbatim_not_tidied(self, vault):
        messy = "  gate  should   fail closed #eng — really  "
        capture_fragment(messy, now=datetime(2026, 8, 23, 9, 14))
        written = _bullets((vault / "Personal" / "Log" / "2026-08-23.md").read_text())
        # Stripped at the ends (it has to sit on a bullet) but otherwise
        # untouched: inner spacing, punctuation, and the hashtag all survive.
        assert written == ["- 09:14 · gate  should   fail closed #eng — really"]

    def test_multiline_fragment_becomes_one_bullet(self, vault):
        capture_fragment("first line\n\n  second line", now=datetime(2026, 8, 23, 9, 14))
        written = (vault / "Personal" / "Log" / "2026-08-23.md").read_text()
        assert _bullets(written) == ["- 09:14 · first line second line"]
        assert len(_bullets(written)) == 1

    def test_day_rolls_over_to_a_new_file(self, vault):
        capture_fragment(_FRAGMENT, now=datetime(2026, 8, 23, 23, 58))
        capture_fragment(_FRAGMENT_2, now=datetime(2026, 8, 24, 0, 3))
        log_dir = vault / "Personal" / "Log"
        assert sorted(p.name for p in log_dir.iterdir()) == ["2026-08-23.md", "2026-08-24.md"]
        assert (log_dir / "2026-08-24.md").read_text().startswith(
            "---\ntype: log\ndate: 2026-08-24\n---\n"
        )

    def test_appends_cleanly_to_a_file_missing_its_trailing_newline(self, vault):
        day = vault / "Personal" / "Log"
        day.mkdir(parents=True)
        # A hand-edited file ending mid-line, last character multi-byte — the
        # tail probe must not glue the new bullet on or fail to decode.
        (day / "2026-08-23.md").write_text(
            "---\ntype: log\ndate: 2026-08-23\n---\n- 09:14 · earlier thought —",
            encoding="utf-8",
        )
        capture_fragment(_FRAGMENT, now=datetime(2026, 8, 23, 14, 37))
        written = (day / "2026-08-23.md").read_text()
        assert _bullets(written) == [
            "- 09:14 · earlier thought —",
            f"- 14:37 · {_FRAGMENT}",
        ]

    def test_empty_fragment_refused_and_writes_nothing(self, vault):
        with pytest.raises(JournalCaptureError):
            capture_fragment("   ")
        assert not (vault / "Personal" / "Log").exists()

    def test_write_failure_raises_rather_than_reporting_success(self, vault):
        # The day dir exists as a *file*, so opening the day file fails.
        (vault / "Personal").mkdir()
        (vault / "Personal" / "Log").write_text("not a directory")
        with pytest.raises(JournalCaptureError):
            capture_fragment(_FRAGMENT)

    def test_error_never_quotes_the_fragment(self, vault):
        secret = "an obviously synthetic private thought"
        (vault / "Personal").mkdir()
        (vault / "Personal" / "Log").write_text("not a directory")
        with pytest.raises(JournalCaptureError) as excinfo:
            capture_fragment(secret)
        assert secret not in str(excinfo.value)
        assert secret not in repr(excinfo.value)
        # `raise ... from None` — nothing the fragment travelled through gets
        # chained in, so a rendered traceback can't carry it either.
        assert excinfo.value.__cause__ is None
        rendered = "".join(traceback.format_exception(
            type(excinfo.value), excinfo.value, excinfo.value.__traceback__,
        ))
        assert secret not in rendered

    def test_fragment_text_never_logged(self, vault, caplog):
        secret = "an obviously synthetic private thought"
        with caplog.at_level("DEBUG"):
            capture_fragment(secret)
        assert secret not in caplog.text

    def test_concurrent_fragments_write_one_header_and_lose_none(self, vault):
        """Twelve fragments racing on the first write of the day. Under the
        exclusive lock exactly one writes the header; the rest append below
        it, and no line is interleaved into another's write."""
        import concurrent.futures

        now = datetime(2026, 8, 23, 9, 14)
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(
                lambda i: capture_fragment(f"synthetic fragment {i:02d}", now=now),
                range(12),
            ))

        written = (vault / "Personal" / "Log" / "2026-08-23.md").read_text()
        assert written.startswith("---\ntype: log\ndate: 2026-08-23\n---\n")
        assert written.count("type: log") == 1
        assert sum(1 for r in results if r.created) == 1
        bullets = _bullets(written)
        assert len(bullets) == 12
        assert sorted(b.split(" · ")[1] for b in bullets) == [
            f"synthetic fragment {i:02d}" for i in range(12)
        ]
        # Every line in the file is either frontmatter or a whole bullet —
        # no partial write spliced into the middle of another.
        body = written.split("---\n", 2)[2]
        assert all(re.fullmatch(r"- \d\d:\d\d · .+", line) for line in body.splitlines())


# ---------------------------------------------------------------------------
# Reserved subtree — Personal/Journal/ stays gsheet_sync's
# ---------------------------------------------------------------------------

class TestReservedJournalDir:
    def test_capture_only_ever_writes_under_personal_log(self, vault):
        capture_fragment(_FRAGMENT, now=datetime(2026, 8, 23, 9, 14))
        assert not (vault / "Personal" / "Journal").exists()
        assert [p.name for p in (vault / "Personal").iterdir()] == ["Log"]

    def test_vault_write_route_still_rejects_the_reserved_prefix(self, vault):
        app = FastAPI()
        app.include_router(vault_route.router)
        client = TestClient(app)
        r = client.post("/api/vault/write", json={
            "path": "Personal/Journal/2026-08-23.md",
            "content": "should never land here",
            "mode": "create",
        })
        assert r.status_code == 400
        assert not (vault / "Personal" / "Journal").exists()


# ---------------------------------------------------------------------------
# End to end through the real chat pipeline (only the model is faked)
# ---------------------------------------------------------------------------

class TestCaptureThroughChatPipeline:
    async def test_fragment_lands_on_disk_via_persona_id(self, vault, journal_persona, fake_model):
        events = await _run_turn(question=_FRAGMENT, persona_id="journal")

        log = _today_log(vault)
        assert log.exists(), "the fragment never reached disk"
        written = log.read_text()
        assert written.startswith(f"---\ntype: log\ndate: {date.today().isoformat()}\n---\n")
        assert [b.split(" · ")[1] for b in _bullets(written)] == [_FRAGMENT]

        capture_events = [e for e in events if e.get("type") == "journal_capture"]
        assert capture_events == [{
            "type": "journal_capture",
            "path": log_path_for(date.today()),
            "created": True,
        }]

    async def test_fragment_lands_via_raw_persona_preamble(self, vault, journal_persona, fake_model):
        """The Telegram bot's path: raw preamble, no persona_id. Reverse
        lookup must find `journal` or capture silently doesn't happen."""
        await _run_turn(question=_FRAGMENT, persona=journal_persona)
        assert _today_log(vault).read_text().rstrip().endswith(f"· {_FRAGMENT}")

    async def test_capture_does_not_depend_on_the_model(self, vault, journal_persona, monkeypatch):
        """The actual #674 failure: the model calls nothing and just talks.
        The fragment must still be on disk."""
        import api.services.agent_loop as agent_loop_mod

        async def prose_only_loop(**kwargs):
            # Exactly what the broken persona produced: the bullet as chat text.
            reply = f"- 21:15 · {_FRAGMENT}\n\nWant a task for that?"
            yield {"type": "text", "content": reply}
            yield {"type": "result", "result": SimpleNamespace(
                total_input_tokens=1, total_output_tokens=1, total_cost_usd=0.0,
                model="fake", tool_calls_log=[], full_text=reply,
            )}

        monkeypatch.setattr(agent_loop_mod, "run_agent_loop", prose_only_loop)

        async def fake_classify(*a, **k):
            return None
        monkeypatch.setattr(chat, "classify_action_intent", fake_classify)

        events = await _run_turn(question=_FRAGMENT, persona_id="journal")

        # The model narrated a bullet it did not write; the file is real.
        assert any(f"- 21:15 · {_FRAGMENT}" in e.get("content", "") for e in events)
        assert [b.split(" · ")[1] for b in _bullets(_today_log(vault).read_text())] == [_FRAGMENT]

    async def test_second_fragment_of_the_day_appends(self, vault, journal_persona, fake_model):
        await _run_turn(question=_FRAGMENT, persona_id="journal")
        events = await _run_turn(question=_FRAGMENT_2, persona_id="journal")

        written = _today_log(vault).read_text()
        assert written.count("type: log") == 1
        assert [b.split(" · ")[1] for b in _bullets(written)] == [_FRAGMENT, _FRAGMENT_2]
        assert [e for e in events if e.get("type") == "journal_capture"][0]["created"] is False

    async def test_non_journal_persona_captures_nothing(self, vault, journal_persona, fake_model):
        events = await _run_turn(question="what did I do last week?")
        assert not (vault / "Personal").exists()
        assert not any(e.get("type") == "journal_capture" for e in events)

    async def test_capture_failure_is_a_clean_error_and_no_stream(
        self, vault, journal_persona, fake_model, monkeypatch,
    ):
        from fastapi import HTTPException

        def boom(text, *, now=None):
            raise JournalCaptureError("could not write Personal/Log/2026-08-23.md")

        monkeypatch.setattr(chat, "capture_fragment", boom)

        with pytest.raises(HTTPException) as excinfo:
            await _run_turn(question=_FRAGMENT, persona_id="journal")
        assert excinfo.value.status_code == 500
        # The reply a user sees must not quote what they said.
        assert _FRAGMENT not in excinfo.value.detail
        assert not (vault / "Personal").exists()


# ---------------------------------------------------------------------------
# End to end through the ring-ingest endpoint on top of the real pipeline
# ---------------------------------------------------------------------------

@pytest.fixture
def ingest_client(tmp_path, monkeypatch, vault, journal_persona, fake_model):
    """`POST /api/journal/ingest` wired to the REAL chat pipeline (via a
    `chat_via_api` that drives `ask_stream` in-process rather than over
    localhost) — so this exercises endpoint → pipeline → file, the chain
    #660's mocked tests could not see through."""
    store = JournalIngestStore(db_path=str(tmp_path / "journal_ingest.db"))
    monkeypatch.setattr(journal_ingest_store, "_store_instance", store)
    journal_ingest._conversations.clear()
    monkeypatch.setattr(journal_ingest.settings, "journal_ingest_token", "secret-token")

    async def real_pipeline(question, conversation_id=None, persona=None):
        events = await _run_turn(
            question=question, conversation_id=conversation_id, persona=persona,
        )
        capture = next((e for e in events if e.get("type") == "journal_capture"), None)
        return {
            "answer": "".join(e.get("content", "") for e in events if e.get("type") == "content"),
            "conversation_id": next(
                e["conversation_id"] for e in events if e.get("type") == "conversation_id"
            ),
            "journal_capture": (
                {"path": capture["path"], "created": capture["created"]} if capture else None
            ),
        }

    monkeypatch.setattr("api.services.telegram.chat_via_api", real_pipeline)

    app = FastAPI()
    app.include_router(journal_ingest.router)
    return TestClient(app), store


def _payload(**overrides):
    payload = {
        "text": _FRAGMENT,
        "device_id": "ring-test-1",
        "timestamp": "2026-08-23T14:37:00Z",
    }
    payload.update(overrides)
    return payload


def _auth():
    return {"Authorization": "Bearer secret-token"}


class TestIngestEndToEnd:
    def test_posted_fragment_appears_in_the_day_log(self, ingest_client, vault):
        client, _ = ingest_client
        resp = client.post("/api/journal/ingest", json=_payload(), headers=_auth())
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "logged"

        written = _today_log(vault).read_text()
        assert written.startswith(f"---\ntype: log\ndate: {date.today().isoformat()}\n---\n")
        assert [b.split(" · ")[1] for b in _bullets(written)] == [_FRAGMENT]

    def test_retried_delivery_logs_exactly_one_bullet(self, ingest_client, vault):
        client, _ = ingest_client
        payload = _payload(text="synthetic retried fragment")
        assert client.post("/api/journal/ingest", json=payload, headers=_auth()).json()["status"] == "logged"
        assert client.post("/api/journal/ingest", json=payload, headers=_auth()).json()["status"] == "duplicate"
        assert len(_bullets(_today_log(vault).read_text())) == 1

    def test_unconfirmed_capture_reports_failure_and_leaves_key_retryable(
        self, ingest_client, vault,
    ):
        """The #674 compounding failure: a capture that didn't happen used to
        return `logged` AND burn the dedupe key, so the retry that would have
        saved the fragment was suppressed forever."""
        client, store = ingest_client
        fragment = "synthetic fragment that must survive a failure"
        payload = _payload(text=fragment)

        def boom(text, *, now=None):
            raise JournalCaptureError("could not write today's log")

        with mock.patch.object(chat, "capture_fragment", boom):
            resp = client.post("/api/journal/ingest", json=payload, headers=_auth())
            assert resp.status_code != 200
            assert not (vault / "Personal").exists()

        # The key was NOT burned: the genuine retry now lands.
        resp = client.post("/api/journal/ingest", json=payload, headers=_auth())
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "logged"
        assert [b.split(" · ")[1] for b in _bullets(_today_log(vault).read_text())] == [fragment]

    def test_pipeline_that_captures_nothing_is_never_reported_as_logged(
        self, ingest_client, vault,
    ):
        """A pipeline that returns a plausible reply but no capture
        confirmation — the exact live symptom — must not be called `logged`,
        and must not burn the key."""
        import api.services.telegram as telegram_mod

        client, store = ingest_client
        payload = _payload(text="synthetic unconfirmed fragment")

        async def talks_but_captures_nothing(question, conversation_id=None, persona=None):
            return {"answer": f"- 21:15 · {question}", "conversation_id": "conv-1"}

        with mock.patch.object(telegram_mod, "chat_via_api", talks_but_captures_nothing):
            resp = client.post("/api/journal/ingest", json=payload, headers=_auth())
            assert resp.status_code == 502
            assert "logged" not in resp.text

        assert client.post("/api/journal/ingest", json=payload, headers=_auth()).json()["status"] == "logged"

    def test_dedupe_store_holds_no_fragment_text(self, ingest_client):
        import sqlite3

        client, store = ingest_client
        secret = "an obviously synthetic private thought"
        client.post("/api/journal/ingest", json=_payload(text=secret), headers=_auth())
        conn = sqlite3.connect(store.db_path)
        rows = conn.execute("SELECT * FROM processed_ingests").fetchall()
        conn.close()
        assert rows and not any(secret in str(cell) for row in rows for cell in row)
