"""
Tests for the /api/tasks/human-queue REST routes (#852).

Uses the real FastAPI app + a real TaskManager against a temp vault (not
mocks) — the behavior under test (dedupe, resolve, done_when validation) is
exactly the kind of thing a mock would paper over.
"""
import pytest
from fastapi.testclient import TestClient

from api.services import human_queue
from api.services.task_manager import TaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def tm(tmp_path, monkeypatch):
    manager = TaskManager(vault_path=tmp_path / "vault", index_path=tmp_path / "task_index.json")
    monkeypatch.setattr(human_queue, "get_task_manager", lambda: manager)
    return manager


@pytest.fixture
def client(tm):
    from api.main import app
    return TestClient(app)


class TestAddRoute:
    def test_add_creates_card(self, client, tm):
        resp = client.post("/api/tasks/human-queue", json={"title": "Re-auth example service"})
        assert resp.status_code == 200
        task_id = resp.json()["id"]
        assert tm.get(task_id).status == "blocked"
        assert tm.get(task_id).tags == ["human"]

    def test_add_stores_source_fields(self, client, tm):
        resp = client.post("/api/tasks/human-queue", json={
            "title": "X",
            "source_host": "example-host",
            "source_cwd": "/home/example",
            "source_session": "sess-1",
        })
        task = tm.get(resp.json()["id"])
        assert task.fields["source_host"] == "example-host"
        assert task.fields["source_cwd"] == "/home/example"
        assert task.fields["source_session"] == "sess-1"

    def test_dedupe_by_open_key_returns_same_id_and_replaces_notes(self, client, tm):
        first = client.post("/api/tasks/human-queue", json={
            "title": "X", "key": "svc-reauth", "notes": "v1",
        }).json()
        second = client.post("/api/tasks/human-queue", json={
            "title": "X", "key": "svc-reauth", "notes": "v2",
        }).json()
        assert second["id"] == first["id"]
        assert tm.get(first["id"]).notes == "v2"
        assert len(tm.list_tasks(tag="human")) == 1

    def test_reopen_after_done_creates_new_card(self, client, tm):
        first = client.post("/api/tasks/human-queue", json={"title": "X", "key": "k"}).json()
        client.put("/api/tasks/human-queue/k/resolve", json={})
        second = client.post("/api/tasks/human-queue", json={"title": "X again", "key": "k"}).json()
        assert second["id"] != first["id"]
        assert tm.get(second["id"]).status == "blocked"

    def test_valid_done_when_endpoint_type_accepted(self, client, tm):
        resp = client.post("/api/tasks/human-queue", json={
            "title": "X",
            "done_when": {"type": "endpoint", "path": "/api/example/status", "pointer": "/status", "equals": "ok"},
        })
        assert resp.status_code == 200

    def test_valid_done_when_file_exists_type_accepted(self, client, tm):
        resp = client.post("/api/tasks/human-queue", json={
            "title": "X",
            "done_when": {"type": "file_exists", "path": "/tmp/example-flag"},
        })
        assert resp.status_code == 200

    def test_invalid_done_when_type_returns_422(self, client, tm):
        resp = client.post("/api/tasks/human-queue", json={
            "title": "X",
            "done_when": {"type": "shell", "command": "echo hi"},
        })
        assert resp.status_code == 422

    def test_done_when_missing_required_key_returns_422(self, client, tm):
        resp = client.post("/api/tasks/human-queue", json={
            "title": "X",
            "done_when": {"type": "endpoint", "path": "/x"},
        })
        assert resp.status_code == 422

    def test_missing_title_returns_400(self, client, tm):
        # title is a required pydantic field with min_length=1 — enforced by
        # FastAPI's own request-shape validation (400 via the global handler
        # in api/main.py), distinct from the manual 422 done_when check.
        resp = client.post("/api/tasks/human-queue", json={})
        assert resp.status_code == 400

    @pytest.mark.parametrize("path", ["@host/x", "//host/x", "http://host/x"])
    def test_done_when_path_authority_injection_returns_422(self, client, tm, path):
        resp = client.post("/api/tasks/human-queue", json={
            "title": "X",
            "done_when": {"type": "endpoint", "path": path, "pointer": "/status", "equals": "ok"},
        })
        assert resp.status_code == 422

    def test_done_when_non_string_path_returns_422(self, client, tm):
        resp = client.post("/api/tasks/human-queue", json={
            "title": "X",
            "done_when": {"type": "endpoint", "path": 5, "pointer": "/status", "equals": "ok"},
        })
        assert resp.status_code == 422

    def test_done_when_list_equals_returns_422(self, client, tm):
        resp = client.post("/api/tasks/human-queue", json={
            "title": "X",
            "done_when": {"type": "endpoint", "path": "/x", "pointer": "/status", "equals": ["ok"]},
        })
        assert resp.status_code == 422

    def test_done_when_bracket_in_pointer_returns_422(self, client, tm):
        resp = client.post("/api/tasks/human-queue", json={
            "title": "X",
            "done_when": {"type": "endpoint", "path": "/x", "pointer": "/a]b", "equals": "ok"},
        })
        assert resp.status_code == 422

    def test_key_with_slash_returns_422(self, client, tm):
        resp = client.post("/api/tasks/human-queue", json={"title": "X", "key": "a/b"})
        assert resp.status_code == 422


class TestListRoute:
    def test_list_returns_open_cards(self, client, tm):
        client.post("/api/tasks/human-queue", json={"title": "Open card", "key": "k1"})
        client.post("/api/tasks/human-queue", json={"title": "Done card", "key": "k2"})
        client.put("/api/tasks/human-queue/k2/resolve", json={})

        resp = client.get("/api/tasks/human-queue")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        titles = {c["title"] for c in body["cards"]}
        assert titles == {"Open card"}

    def test_list_shape(self, client, tm):
        client.post("/api/tasks/human-queue", json={
            "title": "X", "key": "k", "notes": "n",
            "done_when": {"type": "file_exists", "path": "/tmp/f"},
        })
        card = client.get("/api/tasks/human-queue").json()["cards"][0]
        for field in ("id", "title", "key", "age_hours", "source_host",
                      "source_cwd", "source_session", "done_when"):
            assert field in card
        assert card["done_when"] == {"type": "file_exists", "path": "/tmp/f"}

    def test_list_empty(self, client, tm):
        resp = client.get("/api/tasks/human-queue")
        assert resp.json() == {"cards": [], "total": 0}


class TestResolveRoute:
    def test_resolve_by_id(self, client, tm):
        card = client.post("/api/tasks/human-queue", json={"title": "X"}).json()
        resp = client.put(f"/api/tasks/human-queue/{card['id']}/resolve", json={"note": "fixed"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "done"
        assert "fixed" in tm.get(card["id"]).notes

    def test_resolve_by_key(self, client, tm):
        card = client.post("/api/tasks/human-queue", json={"title": "X", "key": "my-key"}).json()
        resp = client.put("/api/tasks/human-queue/my-key/resolve", json={"note": "fixed"})
        assert resp.status_code == 200
        assert resp.json()["id"] == card["id"]

    def test_resolve_unknown_returns_404(self, client, tm):
        resp = client.put("/api/tasks/human-queue/does-not-exist/resolve", json={})
        assert resp.status_code == 404

    def test_resolve_without_note_body(self, client, tm):
        card = client.post("/api/tasks/human-queue", json={"title": "X"}).json()
        resp = client.put(f"/api/tasks/human-queue/{card['id']}/resolve", json={})
        assert resp.status_code == 200

    def test_resolve_by_key_after_refile_succeeds(self, client, tm):
        """Regression: file key -> resolve by key -> refile same key ->
        resolve by key must succeed, not 404 against the now-done card."""
        client.post("/api/tasks/human-queue", json={"title": "X", "key": "sync:gmail"})
        assert client.put("/api/tasks/human-queue/sync:gmail/resolve", json={}).status_code == 200

        second = client.post("/api/tasks/human-queue", json={"title": "X again", "key": "sync:gmail"}).json()
        resp = client.put("/api/tasks/human-queue/sync:gmail/resolve", json={"note": "fixed again"})
        assert resp.status_code == 200
        assert resp.json()["id"] == second["id"]


class TestRouteOrderingRegression:
    """`/human-queue` and `/human-queue/{id_or_key}/resolve` must be matched
    before `/{task_id}` — otherwise FastAPI would treat "human-queue" as a
    task_id and this whole surface would 404/misroute."""

    def test_get_human_queue_not_captured_as_task_id(self, client, tm):
        resp = client.get("/api/tasks/human-queue")
        assert resp.status_code == 200
        assert "cards" in resp.json()

    def test_post_human_queue_not_captured_as_task_id(self, client, tm):
        resp = client.post("/api/tasks/human-queue", json={"title": "X"})
        assert resp.status_code == 200
        assert "id" in resp.json()
