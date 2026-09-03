"""
Tests for api/services/human_queue.py — the shared card-shape/dedupe/resolve
logic behind the REST routes, the native chat tool, and the briefing line.

Uses a real TaskManager against a temp vault (not a mock) since the
dedupe-by-key and resolve-by-id-or-key behavior is the thing under test.
"""
import pytest

from api.services import human_queue
from api.services.task_manager import TaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def tm(tmp_path, monkeypatch):
    manager = TaskManager(vault_path=tmp_path / "vault", index_path=tmp_path / "task_index.json")
    monkeypatch.setattr(human_queue, "get_task_manager", lambda: manager)
    return manager


class TestValidateDoneWhen:
    def test_none_passes_through(self):
        assert human_queue.validate_done_when(None) is None

    def test_endpoint_type_ok(self):
        dw = {"type": "endpoint", "path": "/api/example/status", "pointer": "/status", "equals": "ok"}
        assert human_queue.validate_done_when(dw) == dw

    def test_file_exists_type_ok(self):
        dw = {"type": "file_exists", "path": "/tmp/example-flag"}
        assert human_queue.validate_done_when(dw) == dw

    def test_endpoint_missing_fields_raises(self):
        with pytest.raises(human_queue.DoneWhenError):
            human_queue.validate_done_when({"type": "endpoint", "path": "/x"})

    def test_file_exists_missing_path_raises(self):
        with pytest.raises(human_queue.DoneWhenError):
            human_queue.validate_done_when({"type": "file_exists"})

    def test_unknown_type_raises(self):
        with pytest.raises(human_queue.DoneWhenError):
            human_queue.validate_done_when({"type": "shell", "command": "rm -rf /"})

    def test_non_dict_raises(self):
        with pytest.raises(human_queue.DoneWhenError):
            human_queue.validate_done_when("endpoint")

    @pytest.mark.parametrize("path", ["@host/x", "//host/x", "http://host/x", "host/x"])
    def test_endpoint_path_must_start_with_single_slash(self, path):
        """A path that doesn't start with exactly one '/' could re-parse
        the URL's authority when the worker builds f"{api_base}{path}"
        (e.g. '@evil/x' -> 'http://localhost:8000@evil/x') — a blind SSRF
        primitive reachable by any agent that can file a card."""
        with pytest.raises(human_queue.DoneWhenError):
            human_queue.validate_done_when(
                {"type": "endpoint", "path": path, "pointer": "/status", "equals": "ok"}
            )

    def test_endpoint_non_string_path_raises(self):
        with pytest.raises(human_queue.DoneWhenError):
            human_queue.validate_done_when(
                {"type": "endpoint", "path": ["/x"], "pointer": "/status", "equals": "ok"}
            )

    def test_endpoint_list_equals_raises(self):
        with pytest.raises(human_queue.DoneWhenError):
            human_queue.validate_done_when(
                {"type": "endpoint", "path": "/x", "pointer": "/status", "equals": ["ok"]}
            )

    def test_endpoint_bracket_in_pointer_raises(self):
        with pytest.raises(human_queue.DoneWhenError):
            human_queue.validate_done_when(
                {"type": "endpoint", "path": "/x", "pointer": "/a]b", "equals": "ok"}
            )

    def test_endpoint_bracket_in_string_equals_raises(self):
        with pytest.raises(human_queue.DoneWhenError):
            human_queue.validate_done_when(
                {"type": "endpoint", "path": "/x", "pointer": "/status", "equals": "a]b"}
            )

    def test_endpoint_scalar_equals_types_accepted(self):
        for equals in (1, 1.5, True, None):
            dw = {"type": "endpoint", "path": "/x", "pointer": "/status", "equals": equals}
            assert human_queue.validate_done_when(dw) == dw

    def test_file_exists_non_string_path_raises(self):
        with pytest.raises(human_queue.DoneWhenError):
            human_queue.validate_done_when({"type": "file_exists", "path": 5})

    def test_file_exists_empty_path_raises(self):
        with pytest.raises(human_queue.DoneWhenError):
            human_queue.validate_done_when({"type": "file_exists", "path": ""})

    def test_file_exists_bracket_in_path_raises(self):
        with pytest.raises(human_queue.DoneWhenError):
            human_queue.validate_done_when({"type": "file_exists", "path": "/tmp/a]b"})


class TestAddCard:
    def test_add_creates_blocked_human_task(self, tm):
        task = human_queue.add_card(title="Re-authenticate example service")
        assert task.status == "blocked"
        assert task.tags == ["human"]
        assert task.description == "Re-authenticate example service"

    def test_add_never_touches_status_of_other_tasks(self, tm):
        other = tm.create("Unrelated todo")
        human_queue.add_card(title="Something for the operator")
        assert tm.get(other.id).status == "todo"

    def test_key_with_slash_raises(self, tm):
        """A key is interpolated unescaped into the resolve route's URL
        path segment — '/', '?', '#' would split the path or start a
        query/fragment, making the card unresolvable by key."""
        with pytest.raises(ValueError):
            human_queue.add_card(title="X", key="a/b")

    def test_colon_key_accepted(self, tm):
        task = human_queue.add_card(title="X", key="sync:gmail")
        assert tm.get(task.id).fields.get("key") == "sync:gmail"

    def test_add_stores_source_and_key_fields(self, tm):
        task = human_queue.add_card(
            title="X",
            key="example-key",
            source_host="example-host",
            source_cwd="/home/example/project",
            source_session="sess-123",
        )
        refreshed = tm.get(task.id)
        assert refreshed.fields["key"] == "example-key"
        assert refreshed.fields["source_host"] == "example-host"
        assert refreshed.fields["source_cwd"] == "/home/example/project"
        assert refreshed.fields["source_session"] == "sess-123"

    def test_add_stores_done_when_as_json_field(self, tm):
        dw = {"type": "file_exists", "path": "/tmp/flag"}
        task = human_queue.add_card(title="X", done_when=dw)
        raw = tm.get(task.id).fields["done_when"]
        import json
        assert json.loads(raw) == dw

    def test_add_with_invalid_done_when_raises_before_creating(self, tm):
        with pytest.raises(human_queue.DoneWhenError):
            human_queue.add_card(title="X", done_when={"type": "shell"})
        assert tm.list_tasks(tag="human") == []

    def test_dedupe_open_key_updates_notes_no_duplicate(self, tm):
        first = human_queue.add_card(title="Re-auth", key="svc-reauth", notes="first notes")
        second = human_queue.add_card(title="Re-auth", key="svc-reauth", notes="second notes")
        assert second.id == first.id
        assert len(tm.list_tasks(tag="human")) == 1
        assert tm.get(first.id).notes == "second notes"

    def test_dedupe_advances_updated_at(self, tm):
        first = human_queue.add_card(title="Re-auth", key="svc-reauth", notes="v1")
        first_updated = tm.get(first.id).updated_at
        import time
        time.sleep(0.01)
        human_queue.add_card(title="Re-auth", key="svc-reauth", notes="v2")
        assert tm.get(first.id).updated_at > first_updated

    def test_key_only_matching_a_done_card_opens_a_new_one(self, tm):
        first = human_queue.add_card(title="Re-auth", key="svc-reauth")
        human_queue.resolve_card("svc-reauth", note="fixed")
        assert tm.get(first.id).status == "done"

        second = human_queue.add_card(title="Re-auth again", key="svc-reauth")
        assert second.id != first.id
        assert tm.get(second.id).status == "blocked"
        open_cards = tm.list_tasks(tag="human", status="blocked")
        assert len(open_cards) == 1
        assert open_cards[0].id == second.id


class TestResolveCard:
    def test_resolve_by_id_marks_done_and_appends_note(self, tm):
        task = human_queue.add_card(title="X", notes="original notes")
        resolved = human_queue.resolve_card(task.id, note="fixed it")
        assert resolved.status == "done"
        assert "original notes" in resolved.notes
        assert "fixed it" in resolved.notes

    def test_resolve_by_key(self, tm):
        task = human_queue.add_card(title="X", key="my-key")
        resolved = human_queue.resolve_card("my-key", note="done")
        assert resolved.id == task.id
        assert resolved.status == "done"

    def test_resolve_unknown_id_or_key_returns_none(self, tm):
        assert human_queue.resolve_card("does-not-exist") is None

    def test_resolve_already_done_card_by_key_returns_none(self, tm):
        human_queue.add_card(title="X", key="k")
        human_queue.resolve_card("k")
        assert human_queue.resolve_card("k") is None

    def test_resolve_does_not_touch_non_human_task(self, tm):
        other = tm.create("Unrelated task", tags=["work"])
        assert human_queue.resolve_card(other.id) is None
        assert tm.get(other.id).status == "todo"

    def test_resolve_without_note_still_marks_done(self, tm):
        task = human_queue.add_card(title="X")
        resolved = human_queue.resolve_card(task.id)
        assert resolved.status == "done"
        assert resolved.notes

    def test_resolve_by_key_after_refile_succeeds(self, tm):
        """Regression: once a key has both a done card (from the first
        resolve) and a reopened open card (from refiling), resolve-by-key
        must find the open one, not 404 against the done one."""
        first = human_queue.add_card(title="X", key="svc-reauth")
        assert human_queue.resolve_card("svc-reauth", note="fixed").id == first.id

        second = human_queue.add_card(title="X again", key="svc-reauth")
        assert second.id != first.id

        resolved = human_queue.resolve_card("svc-reauth", note="fixed again")
        assert resolved is not None
        assert resolved.id == second.id
        assert resolved.status == "done"


class TestListOpenCards:
    def test_list_only_returns_open_human_cards(self, tm):
        open_card = human_queue.add_card(title="Open one")
        done_card = human_queue.add_card(title="Done one", key="k2")
        human_queue.resolve_card("k2")
        tm.create("Unrelated", tags=["work"])

        cards = human_queue.list_open_cards()
        ids = {c["id"] for c in cards}
        assert open_card.id in ids
        assert done_card.id not in ids
        assert len(cards) == 1

    def test_list_shape(self, tm):
        human_queue.add_card(
            title="X", key="k", notes="n",
            source_host="h", source_cwd="/cwd", source_session="s",
            done_when={"type": "file_exists", "path": "/tmp/f"},
        )
        card = human_queue.list_open_cards()[0]
        for field in ("id", "title", "key", "notes", "age_hours", "source_host",
                      "source_cwd", "source_session", "done_when"):
            assert field in card
        assert card["key"] == "k"
        assert card["source_host"] == "h"
        assert card["source_cwd"] == "/cwd"
        assert card["source_session"] == "s"
        assert card["done_when"] == {"type": "file_exists", "path": "/tmp/f"}
        assert card["age_hours"] is not None and card["age_hours"] >= 0

    def test_list_empty(self, tm):
        assert human_queue.list_open_cards() == []


class TestOpenCardsOlderThan:
    def test_fresh_card_not_older_than_24h(self, tm):
        human_queue.add_card(title="Fresh")
        assert human_queue.open_cards_older_than(24) == []

    def test_backdated_card_is_older_than_threshold(self, tm):
        task = human_queue.add_card(title="Old")
        from datetime import datetime, timedelta, timezone
        backdated = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        # Directly backdate updated_at via the manager's in-memory state to
        # simulate a card that's been open for two days — TaskManager always
        # stamps `updated_at` to "now" on create/update, so there's no public
        # API to file a pre-aged card.
        tm._tasks[task.id].updated_at = backdated
        old = human_queue.open_cards_older_than(24)
        assert len(old) == 1
        assert old[0]["id"] == task.id
