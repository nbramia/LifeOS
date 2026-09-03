"""
Tests for the Human-queue integration in scripts/run_all_syncs.py (#852):
filing a keyed card for a classified sync failure, resolving it on the next
success, and filing the monarch-reauth card on an expired/missing session.

All HTTP calls are monkeypatched (urllib.request.urlopen) — no network, and
no dependency on a running API server.
"""
import json
from urllib.parse import urlparse

import pytest

from scripts import run_all_syncs as ras

pytestmark = pytest.mark.unit


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fake_urlopen(monkeypatch, calls, responses=None, raise_on=None):
    """`responses`: {(method, path): body_dict}. `raise_on`: set of
    (method, path) that should raise instead of responding."""
    responses = responses or {}
    raise_on = raise_on or set()

    def fake_urlopen(req, timeout=None):
        method = req.get_method()
        path = urlparse(req.full_url).path
        payload = json.loads(req.data) if req.data else None
        calls.append((method, path, payload))
        if (method, path) in raise_on:
            raise OSError("connection refused")
        return _FakeResponse(responses.get((method, path), {}))

    monkeypatch.setattr(ras.urllib.request, "urlopen", fake_urlopen)


class TestHumanQueueRequestHelpers:
    def test_file_card_posts_title_notes_key(self, monkeypatch):
        calls = []
        _install_fake_urlopen(monkeypatch, calls)
        ras._human_queue_file_card(title="X", notes="why", key="k1")
        assert calls == [("POST", "/api/tasks/human-queue", {"title": "X", "notes": "why", "key": "k1"})]

    def test_file_card_includes_done_when(self, monkeypatch):
        calls = []
        _install_fake_urlopen(monkeypatch, calls)
        dw = {"type": "file_exists", "path": "/tmp/f"}
        ras._human_queue_file_card(title="X", done_when=dw)
        assert calls[0][2]["done_when"] == dw

    def test_resolve_key_puts_note(self, monkeypatch):
        calls = []
        _install_fake_urlopen(monkeypatch, calls)
        ras._human_queue_resolve_key("sync:example", note="sync succeeded")
        assert calls == [("PUT", "/api/tasks/human-queue/sync:example/resolve", {"note": "sync succeeded"})]

    def test_open_keys_parses_cards(self, monkeypatch):
        calls = []
        _install_fake_urlopen(monkeypatch, calls, responses={
            ("GET", "/api/tasks/human-queue"): {"cards": [{"key": "sync:a"}, {"key": None}, {"key": "sync:b"}]},
        })
        assert ras._human_queue_open_keys() == {"sync:a", "sync:b"}

    def test_request_failure_returns_none_never_raises(self, monkeypatch):
        calls = []
        _install_fake_urlopen(monkeypatch, calls, raise_on={("GET", "/api/tasks/human-queue")})
        assert ras._human_queue_open_keys() == set()


class TestFileHumanQueueCardsForSync:
    def test_failed_source_files_keyed_card_with_error_text(self, monkeypatch):
        calls = []
        _install_fake_urlopen(monkeypatch, calls, responses={
            ("GET", "/api/tasks/human-queue"): {"cards": []},
        })
        result = {
            "failed_sources": ["gmail"],
            "results": {"gmail": {"success": False, "error": "401 Unauthorized"}},
        }
        ras.file_human_queue_cards_for_sync(result)
        posts = [c for c in calls if c[0] == "POST"]
        assert len(posts) == 1
        _, path, payload = posts[0]
        assert path == "/api/tasks/human-queue"
        assert payload["key"] == "sync:gmail"
        assert "401 Unauthorized" in payload["notes"]

    def test_missing_error_text_defaults_to_generic_message(self, monkeypatch):
        calls = []
        _install_fake_urlopen(monkeypatch, calls, responses={
            ("GET", "/api/tasks/human-queue"): {"cards": []},
        })
        result = {"failed_sources": ["calendar"], "results": {"calendar": {"success": False}}}
        ras.file_human_queue_cards_for_sync(result)
        payload = next(c[2] for c in calls if c[0] == "POST")
        assert payload["notes"] == "sync failed"

    def test_succeeding_source_with_open_card_is_resolved(self, monkeypatch):
        calls = []
        _install_fake_urlopen(monkeypatch, calls, responses={
            ("GET", "/api/tasks/human-queue"): {"cards": [{"key": "sync:slack"}]},
        })
        result = {"failed_sources": [], "results": {"slack": {"success": True}}}
        ras.file_human_queue_cards_for_sync(result)
        puts = [c for c in calls if c[0] == "PUT"]
        assert puts == [("PUT", "/api/tasks/human-queue/sync:slack/resolve", {"note": "sync succeeded"})]

    def test_succeeding_source_without_open_card_triggers_no_resolve_call(self, monkeypatch):
        """Efficiency: don't fire a resolve (and its inevitable 404) for
        every healthy source on every run — only ones with an actual open
        card."""
        calls = []
        _install_fake_urlopen(monkeypatch, calls, responses={
            ("GET", "/api/tasks/human-queue"): {"cards": []},
        })
        result = {"failed_sources": [], "results": {"gmail": {"success": True}, "calendar": {"success": True}}}
        ras.file_human_queue_cards_for_sync(result)
        assert [c for c in calls if c[0] == "PUT"] == []

    def test_failed_source_is_never_also_resolved(self, monkeypatch):
        calls = []
        _install_fake_urlopen(monkeypatch, calls, responses={
            ("GET", "/api/tasks/human-queue"): {"cards": [{"key": "sync:gmail"}]},
        })
        result = {"failed_sources": ["gmail"], "results": {"gmail": {"success": False, "error": "boom"}}}
        ras.file_human_queue_cards_for_sync(result)
        assert [c for c in calls if c[0] == "PUT"] == []

    def test_skipped_source_with_open_card_is_not_resolved(self, monkeypatch):
        """A pre-run skip (recently_synced, disabled, monthly-not-due, ...)
        never ran, so its open card must stay open, not be marked 'sync
        succeeded' just because the source isn't in failed_sources."""
        calls = []
        _install_fake_urlopen(monkeypatch, calls, responses={
            ("GET", "/api/tasks/human-queue"): {"cards": [{"key": "sync:gmail"}]},
        })
        result = {
            "failed_sources": [],
            "results": {"gmail": {"skipped": True, "reason": "recently_synced"}},
        }
        ras.file_human_queue_cards_for_sync(result)
        assert [c for c in calls if c[0] == "PUT"] == []

    def test_dependency_skipped_source_with_open_card_is_not_resolved(self, monkeypatch):
        calls = []
        _install_fake_urlopen(monkeypatch, calls, responses={
            ("GET", "/api/tasks/human-queue"): {"cards": [{"key": "sync:calendar"}]},
        })
        result = {
            "failed_sources": [],
            "results": {
                "calendar": {
                    "skipped": True,
                    "reason": "dependency_failed",
                    "failed_dependencies": ["gmail"],
                }
            },
        }
        ras.file_human_queue_cards_for_sync(result)
        assert [c for c in calls if c[0] == "PUT"] == []

    def test_never_raises_even_if_every_call_fails(self, monkeypatch):
        calls = []
        _install_fake_urlopen(
            monkeypatch, calls,
            raise_on={("GET", "/api/tasks/human-queue"), ("POST", "/api/tasks/human-queue")},
        )
        result = {"failed_sources": ["gmail"], "results": {"gmail": {"success": False, "error": "x"}}}
        ras.file_human_queue_cards_for_sync(result)  # must not raise


class TestFileHumanQueueCardForMonarch:
    def test_expired_files_card_with_done_when(self, monkeypatch):
        calls = []
        _install_fake_urlopen(monkeypatch, calls)
        ras.file_human_queue_card_for_monarch({"status": "expired", "message": "session is 45d old"})
        assert len(calls) == 1
        method, path, payload = calls[0]
        assert method == "POST" and path == "/api/tasks/human-queue"
        assert payload["key"] == "monarch-reauth"
        assert payload["notes"] == "session is 45d old"
        assert payload["done_when"] == {
            "type": "endpoint",
            "path": "/api/monarch/session_status",
            "pointer": "/status",
            "equals": "ok",
        }

    def test_missing_files_card(self, monkeypatch):
        calls = []
        _install_fake_urlopen(monkeypatch, calls)
        ras.file_human_queue_card_for_monarch({"status": "missing", "message": "no session"})
        assert len(calls) == 1

    def test_ok_status_does_not_file(self, monkeypatch):
        calls = []
        _install_fake_urlopen(monkeypatch, calls)
        ras.file_human_queue_card_for_monarch({"status": "ok", "message": "fine"})
        assert calls == []

    def test_expiring_soon_does_not_file(self, monkeypatch):
        """Not yet operator-actionable — only expired/missing should file."""
        calls = []
        _install_fake_urlopen(monkeypatch, calls)
        ras.file_human_queue_card_for_monarch({"status": "expiring_soon", "message": "soon"})
        assert calls == []

    def test_never_raises_on_request_failure(self, monkeypatch):
        calls = []
        _install_fake_urlopen(monkeypatch, calls, raise_on={("POST", "/api/tasks/human-queue")})
        ras.file_human_queue_card_for_monarch({"status": "expired", "message": "x"})  # must not raise
