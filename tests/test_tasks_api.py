"""
Tests for the Tasks API routes.

Tests CRUD endpoints with mocked TaskManager.
"""
import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


class TestTasksAPI:
    """Tests for the /api/tasks endpoints."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    @pytest.fixture(autouse=True)
    def _isolated_session_store(self, tmp_path, monkeypatch):
        """The claimed-card tags guard queries `SessionStore.has_live_session`
        (see `api/routes/tasks.py`'s tags-patch check) whenever a tags
        patch touches the assignee-tag or claim-tag set — point that at a
        temp-dir-backed store instead of the real one for every test in
        this class, so a test never opens (or, worse, matches against) the
        real `data/agent_sessions.db`. Empty by default, which makes
        `has_live_session` return False for any task id — exactly what
        every existing test in this file already assumes."""
        from api.routes import tasks as tasks_route
        from api.services.agent_worker.session_store import SessionStore
        monkeypatch.setattr(tasks_route, "_session_store", SessionStore(db_path=tmp_path / "sessions.db"))

    @pytest.fixture
    def mock_task_manager(self):
        with patch("api.routes.tasks.get_task_manager") as mock:
            from api.services.task_manager import Task

            manager = mock.return_value

            sample_task = Task(
                id="abc12345",
                description="Pull 1099 from Schwab",
                status="todo",
                context="Finance",
                priority="medium",
                due_date="2025-02-15",
                created_date="2025-02-08",
                tags=["tax", "schwab"],
                source_file="LifeOS/Tasks/Finance.md",
                line_number=7,
            )

            manager.create.return_value = sample_task
            manager.get.return_value = sample_task
            manager.list_tasks.return_value = [sample_task]
            manager.update.return_value = sample_task
            manager.complete.return_value = sample_task
            manager.delete.return_value = True
            yield manager

    # --- CREATE ---

    def test_create_task(self, client, mock_task_manager):
        response = client.post("/api/tasks", json={
            "description": "Pull 1099 from Schwab",
            "tags": ["tax", "schwab"],
        })
        assert response.status_code == 200
        data = response.json()
        assert data["description"] == "Pull 1099 from Schwab"
        assert data["id"] == "abc12345"
        # No context in the request -> defaults to Inbox.
        _, kwargs = mock_task_manager.create.call_args
        assert kwargs["context"] == "Inbox"

    def test_create_task_forwards_context(self, client, mock_task_manager):
        """#853: unlike the chat `manage_tasks` tool (which always lands in
        Inbox to avoid an LLM guessing a wrong context), the raw HTTP API
        honors an explicit context on create — a direct API client (the
        Kanban board) knows exactly where it wants the task filed. This
        intentionally replaces the old test_create_task_ignores_context_input,
        which pinned the opposite behavior at this layer."""
        response = client.post("/api/tasks", json={
            "description": "Quick task",
            "context": "Work",
        })
        assert response.status_code == 200
        _, kwargs = mock_task_manager.create.call_args
        assert kwargs["context"] == "Work"

    def test_create_task_forwards_status_notes_fields(self, client, mock_task_manager):
        response = client.post("/api/tasks", json={
            "description": "Rich task",
            "status": "blocked",
            "notes": "line one\nline two",
            "fields": {"host": "laptop", "effort": "high"},
        })
        assert response.status_code == 200
        _, kwargs = mock_task_manager.create.call_args
        assert kwargs["status"] == "blocked"
        assert kwargs["notes"] == "line one\nline two"
        assert kwargs["fields"] == {"host": "laptop", "effort": "high"}

    def test_create_task_invalid_status_is_422(self, client, mock_task_manager):
        response = client.post("/api/tasks", json={
            "description": "Bad status",
            "status": "not_a_real_status",
        })
        assert response.status_code == 422
        mock_task_manager.create.assert_not_called()

    def test_create_task_minimal(self, client, mock_task_manager):
        response = client.post("/api/tasks", json={
            "description": "Quick task",
        })
        assert response.status_code == 200
        mock_task_manager.create.assert_called_once()

    def test_create_task_empty_description(self, client, mock_task_manager):
        response = client.post("/api/tasks", json={
            "description": "",
        })
        assert response.status_code in (400, 422)  # Validation error

    def test_create_task_failure_is_never_success_shaped(self, mock_task_manager):
        """#609: if the write itself fails (disk error, index corruption,
        whatever `TaskManager.create` raises for), the caller must see a
        non-2xx status — never a 200 with the created task's own shape,
        which is the false-confirmation pattern this issue exists to close.

        Uses `raise_server_exceptions=False` because this route has no
        try/except of its own — it relies on FastAPI's default unhandled-
        exception -> 500 behavior, which the default TestClient re-raises
        into the test instead of returning, for debuggability."""
        from fastapi.testclient import TestClient
        from api.main import app

        mock_task_manager.create.side_effect = OSError("disk write failed")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/api/tasks", json={"description": "Pull 1099 from Schwab"})
        assert not (200 <= response.status_code < 300)

    def test_create_task_conflict_is_409(self, client, mock_task_manager):
        from api.services.task_manager import TaskConflictError
        mock_task_manager.create.side_effect = TaskConflictError("too many conflicting writes")
        response = client.post("/api/tasks", json={"description": "Pull 1099 from Schwab"})
        assert response.status_code == 409

    def test_create_task_hostile_field_value_is_422(self, client, mock_task_manager):
        """#853 round 1 finding #2: a `ValueError` from `TaskManager.create`
        (hostile description/notes/fields content) maps to 422, not an
        unhandled 500."""
        mock_task_manager.create.side_effect = ValueError("description must not contain '\\n'")
        response = client.post("/api/tasks", json={"description": "Pull 1099 from Schwab"})
        assert response.status_code == 422
        assert "must not contain" in response.json()["detail"]

    def test_create_task_reserved_field_key_is_422(self, client, mock_task_manager):
        """#853 round 1 finding #3: a reserved `fields` key (e.g. `updated`)
        maps to 422."""
        mock_task_manager.create.side_effect = ValueError(
            "'updated' is a reserved field and cannot be set via fields"
        )
        response = client.post("/api/tasks", json={
            "description": "Pull 1099 from Schwab",
            "fields": {"updated": "SPOOFED"},
        })
        assert response.status_code == 422
        assert "reserved" in response.json()["detail"]

    # --- DRY RUN (#138) ---

    def test_dry_run_with_agent_tag_returns_preflight_preview(self, client, mock_task_manager):
        """dry_run=true on an #agent task runs preflight and returns the
        routing decision + cost estimate WITHOUT creating the task."""
        from api.services.agent_worker.preflight import (
            PreflightBudget,
            PreflightResult,
            ROUTE_CLAUDE,
        )
        fake_result = PreflightResult(
            budget=PreflightBudget(wall_seconds=600, max_tokens=20_000, max_dollars=2.0),
            routing=ROUTE_CLAUDE,
            routing_reason="multi-step task; needs cloud",
            expected_output="text",
            ambiguity=None,
            sane=True,
            sane_reason="",
        )
        with patch("api.services.agent_worker.preflight.run_preflight", return_value=fake_result):
            response = client.post("/api/tasks", json={
                "description": "draft my Q4 review",
                "tags": ["agent"],
                "dry_run": True,
            })
        assert response.status_code == 200
        data = response.json()
        assert data["dry_run"] is True
        assert data["routing"] == "claude"
        assert data["budget"]["max_dollars"] == 2.0
        assert data["estimated_dollars"] > 0
        # The dry-run path does NOT touch the task manager.
        mock_task_manager.create.assert_not_called()

    def test_dry_run_remote_route_prices_from_remote_settings(self, client, mock_task_manager, monkeypatch):
        """(#809) `#cloud`'s dry-run preview prices from
        `settings.remote_llm_{input,output}_price_per_mtok` — never the
        Anthropic `cost_for` table `ROUTE_CLAUDE` uses."""
        from config.settings import settings
        from api.services.agent_worker.preflight import (
            PreflightBudget,
            PreflightResult,
            ROUTE_REMOTE,
        )
        monkeypatch.setattr(settings, "remote_llm_input_price_per_mtok", 0.27, raising=False)
        monkeypatch.setattr(settings, "remote_llm_output_price_per_mtok", 1.10, raising=False)
        fake_result = PreflightResult(
            budget=PreflightBudget(wall_seconds=600, max_tokens=20_000, max_dollars=2.0),
            routing=ROUTE_REMOTE,
            routing_reason="#cloud tag present",
            expected_output="text",
            ambiguity=None,
            sane=True,
            sane_reason="",
        )
        with patch("api.services.agent_worker.preflight.run_preflight", return_value=fake_result):
            response = client.post("/api/tasks", json={
                "description": "draft my Q4 review",
                "tags": ["agent", "cloud"],
                "dry_run": True,
            })
        assert response.status_code == 200
        data = response.json()
        assert data["routing"] == "remote"
        expected = (10_000 / 1_000_000) * 0.27 + (10_000 / 1_000_000) * 1.10
        assert data["estimated_dollars"] == pytest.approx(expected)

    def test_dry_run_remote_route_floors_at_zero_when_unpriced(self, client, mock_task_manager, monkeypatch):
        """(#809) Unset remote rates mean 'unknown, not free' (#669) — the
        dry-run estimate floors at 0 rather than guessing a rate, the same
        convention actual spend recording uses."""
        from config.settings import settings
        from api.services.agent_worker.preflight import (
            PreflightBudget,
            PreflightResult,
            ROUTE_REMOTE,
        )
        monkeypatch.setattr(settings, "remote_llm_input_price_per_mtok", None, raising=False)
        monkeypatch.setattr(settings, "remote_llm_output_price_per_mtok", None, raising=False)
        fake_result = PreflightResult(
            budget=PreflightBudget(wall_seconds=600, max_tokens=20_000, max_dollars=2.0),
            routing=ROUTE_REMOTE,
            routing_reason="#cloud tag present",
            expected_output="text",
            ambiguity=None,
            sane=True,
            sane_reason="",
        )
        with patch("api.services.agent_worker.preflight.run_preflight", return_value=fake_result):
            response = client.post("/api/tasks", json={
                "description": "draft my Q4 review",
                "tags": ["agent", "cloud"],
                "dry_run": True,
            })
        assert response.status_code == 200
        assert response.json()["estimated_dollars"] == 0.0

    def test_dry_run_without_agent_tag_falls_through_to_create(self, client, mock_task_manager):
        """dry_run is a no-op for non-#agent tasks — the task is still created."""
        response = client.post("/api/tasks", json={
            "description": "shopping list",
            "dry_run": True,
        })
        assert response.status_code == 200
        # Falls through to real task creation.
        mock_task_manager.create.assert_called_once()

    def test_dry_run_false_creates_task_normally(self, client, mock_task_manager):
        """dry_run=false on an #agent task still creates it (the worker
        picks it up later and does its own preflight)."""
        response = client.post("/api/tasks", json={
            "description": "dispatch this",
            "tags": ["agent"],
            "dry_run": False,
        })
        assert response.status_code == 200
        mock_task_manager.create.assert_called_once()

    # --- LIST ---

    def test_list_tasks(self, client, mock_task_manager):
        response = client.get("/api/tasks")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["description"] == "Pull 1099 from Schwab"

    def test_list_tasks_with_filters(self, client, mock_task_manager):
        response = client.get("/api/tasks?status=todo&context=Finance&tag=tax")
        assert response.status_code == 200
        mock_task_manager.list_tasks.assert_called_once_with(
            status="todo",
            context="Finance",
            tag="tax",
            due_before=None,
            query=None,
        )

    def test_list_tasks_with_query(self, client, mock_task_manager):
        response = client.get("/api/tasks?query=taxes")
        assert response.status_code == 200
        mock_task_manager.list_tasks.assert_called_once_with(
            status=None,
            context=None,
            tag=None,
            due_before=None,
            query="taxes",
        )

    def test_list_tasks_empty(self, client, mock_task_manager):
        mock_task_manager.list_tasks.return_value = []
        response = client.get("/api/tasks")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["tasks"] == []

    # --- GET ---

    def test_get_task(self, client, mock_task_manager):
        response = client.get("/api/tasks/abc12345")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "abc12345"
        assert data["description"] == "Pull 1099 from Schwab"

    def test_get_task_not_found(self, client, mock_task_manager):
        mock_task_manager.get.return_value = None
        response = client.get("/api/tasks/nonexistent")
        assert response.status_code == 404

    # --- UPDATE ---

    def test_update_task(self, client, mock_task_manager):
        response = client.put("/api/tasks/abc12345", json={
            "priority": "high",
        })
        assert response.status_code == 200
        mock_task_manager.update.assert_called_once()

    def test_update_task_status(self, client, mock_task_manager):
        response = client.put("/api/tasks/abc12345", json={
            "status": "in_progress",
        })
        assert response.status_code == 200

    def test_update_task_not_found(self, client, mock_task_manager):
        mock_task_manager.update.return_value = None
        response = client.put("/api/tasks/nonexistent", json={
            "priority": "low",
        })
        assert response.status_code == 404

    def test_update_task_invalid_status_is_422(self, client, mock_task_manager):
        response = client.put("/api/tasks/abc12345", json={
            "status": "not_a_real_status",
        })
        assert response.status_code == 422
        mock_task_manager.update.assert_not_called()

    def test_update_task_forwards_notes_and_fields(self, client, mock_task_manager):
        response = client.put("/api/tasks/abc12345", json={
            "notes": "new notes",
            "fields": {"host": "laptop", "effort": None},
        })
        assert response.status_code == 200
        _, kwargs = mock_task_manager.update.call_args
        assert kwargs["notes"] == "new notes"
        assert kwargs["fields"] == {"host": "laptop", "effort": None}

    def test_update_task_conflict_is_409(self, client, mock_task_manager):
        from api.services.task_manager import TaskConflictError
        mock_task_manager.update.side_effect = TaskConflictError("too many conflicting writes")
        response = client.put("/api/tasks/abc12345", json={"priority": "high"})
        assert response.status_code == 409

    def test_update_task_hostile_field_value_is_422(self, client, mock_task_manager):
        """#853 round 1 finding #2: a `ValueError` from `TaskManager.update`
        maps to 422, not an unhandled 500."""
        mock_task_manager.update.side_effect = ValueError("fields['x'] must not contain ']'")
        response = client.put("/api/tasks/abc12345", json={"fields": {"x": "bad]value"}})
        assert response.status_code == 422
        assert "must not contain" in response.json()["detail"]

    # --- claimed-card tags guard: keyed on the card's own claim state,
    # not on the `assigned_by: board` marker (that marker only gates the
    # model/effort/host check below, since the drawer's Tags field sends a
    # bare `{"tags": [...]}` patch with no `fields` key at all) ---

    def _set_current(self, mock_task_manager, *, status="todo", tags=None):
        from api.services.task_manager import Task
        current = Task(
            id="abc12345", description="Pull 1099 from Schwab", status=status,
            tags=list(tags or []), source_file="LifeOS/Tasks/Finance.md", line_number=7,
        )
        mock_task_manager.get.return_value = current
        return current

    def test_board_marker_field_edit_on_claimed_card_is_409_and_nothing_written(self, client, mock_task_manager):
        self._set_current(mock_task_manager, tags=["codex", "agent-running"])
        response = client.put("/api/tasks/abc12345", json={
            "fields": {"effort": "high", "host": None, "assigned_by": "board"},
        })
        assert response.status_code == 409
        assert "answer or kill the session first" in response.json()["detail"]
        mock_task_manager.update.assert_not_called()

    def test_board_marker_assignee_change_on_claimed_card_is_409_and_nothing_written(self, client, mock_task_manager):
        self._set_current(mock_task_manager, tags=["codex", "agent-blocked"])
        response = client.put("/api/tasks/abc12345", json={
            "tags": ["hermes"],
            "fields": {"effort": None, "host": None, "assigned_by": "board"},
        })
        assert response.status_code == 409
        assert "answer or kill the session first" in response.json()["detail"]
        mock_task_manager.update.assert_not_called()

    def test_board_marker_status_change_on_claimed_card_is_409_and_nothing_written(
        self, client, mock_task_manager,
    ):
        """A board-marked patch carrying a raw `status` field reaches the
        same claimed-card guard as model/effort/host — the board itself
        only ever moves lanes through the lane endpoint, but nothing else
        stops a `status` write from carrying the marker too, and a claimed
        card's status is exactly what the worker owns while running."""
        self._set_current(mock_task_manager, tags=["codex", "agent-running"])
        response = client.put("/api/tasks/abc12345", json={
            "status": "done", "fields": {"assigned_by": "board"},
        })
        assert response.status_code == 409
        assert "answer or kill the session first" in response.json()["detail"]
        mock_task_manager.update.assert_not_called()

    def test_board_marker_status_change_on_unclaimed_agent_card_succeeds(self, client, mock_task_manager):
        """Positive case: the identical board-marked status patch on an
        UNCLAIMED agent card is unaffected."""
        self._set_current(mock_task_manager, tags=["codex"])
        response = client.put("/api/tasks/abc12345", json={
            "status": "done", "fields": {"assigned_by": "board"},
        })
        assert response.status_code == 200
        mock_task_manager.update.assert_called_once()

    def test_unmarked_status_change_on_claimed_card_skips_the_guard(self, client, mock_task_manager):
        """A `status` patch with no `assigned_by: board` marker and no
        `tags` key never reaches the field_edit guard at all — matching
        every agent-side/vault-side status write today (only a
        board-marked write, or a `tags` patch, pays for the extra read)."""
        response = client.put("/api/tasks/abc12345", json={"status": "done"})
        assert response.status_code == 200
        mock_task_manager.get.assert_not_called()
        mock_task_manager.update.assert_called_once()

    @pytest.mark.parametrize("lifecycle_tag", ["agent-completed", "accepted"])
    def test_unmarked_tags_patch_adding_agent_completed_or_accepted_is_409(
        self, client, mock_task_manager, lifecycle_tag,
    ):
        """`agent-completed` and `accepted` are just as unreachable through
        a bare tags PUT as the two claim tags — the worker writes the
        former, the accept endpoint writes the latter, and no other HTTP
        caller in this codebase adds either. Manufacturing one this way
        would fake a Review state (or a fake accept out of one)."""
        self._set_current(mock_task_manager, tags=["codex"])
        response = client.put("/api/tasks/abc12345", json={"tags": ["codex", lifecycle_tag]})
        assert response.status_code == 409
        mock_task_manager.update.assert_not_called()

    def test_unmarked_tags_patch_without_agent_completed_or_accepted_succeeds(self, client, mock_task_manager):
        """Positive case: an ordinary tags edit that adds neither
        lifecycle tag is unaffected."""
        self._set_current(mock_task_manager, tags=["codex"])
        response = client.put("/api/tasks/abc12345", json={"tags": ["codex", "needs-design"]})
        assert response.status_code == 200
        mock_task_manager.update.assert_called_once()

    def test_board_marker_case_insensitive_and_stripped(self, client, mock_task_manager):
        self._set_current(mock_task_manager, tags=["codex", "agent-running"])
        response = client.put("/api/tasks/abc12345", json={
            "fields": {"model": "claude-opus-5", "assigned_by": "  Board  "},
        })
        assert response.status_code == 409
        mock_task_manager.update.assert_not_called()

    def test_fields_only_patch_without_board_marker_succeeds_on_claimed_card(self, client, mock_task_manager):
        """A model/effort/host patch with no `assigned_by: board` marker
        and no `tags` key -> neither guard runs, matching agent-side/
        vault-side field writes exactly. (The tags guard below is
        marker-independent; this test has no `tags` key at all, so it
        never reaches that check either.)"""
        self._set_current(mock_task_manager, tags=["codex", "agent-running"])
        response = client.put("/api/tasks/abc12345", json={
            "fields": {"effort": "high", "host": None},
        })
        assert response.status_code == 200
        mock_task_manager.update.assert_called_once()

    def test_board_marker_field_edit_on_unclaimed_agent_card_succeeds(self, client, mock_task_manager):
        self._set_current(mock_task_manager, tags=["codex"])
        response = client.put("/api/tasks/abc12345", json={
            "fields": {"effort": "high", "host": None, "assigned_by": "board"},
        })
        assert response.status_code == 200
        mock_task_manager.update.assert_called_once()

    def test_board_marker_assignee_change_on_unclaimed_agent_card_succeeds(self, client, mock_task_manager):
        self._set_current(mock_task_manager, tags=["codex"])
        response = client.put("/api/tasks/abc12345", json={
            "tags": ["hermes"],
            "fields": {"effort": None, "host": None, "assigned_by": "board"},
        })
        assert response.status_code == 200
        mock_task_manager.update.assert_called_once()

    def test_board_marker_without_model_effort_host_or_assignee_change_skips_guard(self, client, mock_task_manager):
        """The marker alone doesn't trigger a refusal — only an actual
        model/effort/host change or an assignee-changing tag patch does."""
        self._set_current(mock_task_manager, tags=["codex", "agent-running"])
        response = client.put("/api/tasks/abc12345", json={
            "fields": {"assigned_by": "board"},
        })
        assert response.status_code == 200
        mock_task_manager.update.assert_called_once()

    def test_board_marker_tags_and_claim_state_unchanged_skips_assignee_check(self, client, mock_task_manager):
        """The same assignee tag AND the same claim tag re-sent (no actual
        assignee or claim-state change, just an unrelated extra label) must
        not trip the assignee_change guard even on a claimed card."""
        self._set_current(mock_task_manager, tags=["codex", "agent-running"])
        response = client.put("/api/tasks/abc12345", json={
            "tags": ["codex", "agent-running", "extra-label"],
            "fields": {"assigned_by": "board"},
        })
        assert response.status_code == 200
        mock_task_manager.update.assert_called_once()

    def test_unmarked_tags_patch_dropping_claim_tag_on_claimed_card_is_409(self, client, mock_task_manager):
        """A bare `{"tags": [...]}` PUT — no `fields` key at all, exactly
        the shape the drawer's Tags field blur handler sends — that leaves
        the derived assignee unchanged but silently drops `agent-running`
        must be refused just like an explicit assignee change: the derived
        assignee ("codex") doesn't change, only the claim tag disappears.
        The guard runs on the card's own claim state regardless of any
        marker, so this request shape — the one the product actually
        sends — is caught."""
        self._set_current(mock_task_manager, tags=["codex", "agent-running"])
        response = client.put("/api/tasks/abc12345", json={"tags": ["codex", "extra-label"]})
        assert response.status_code == 409
        assert "answer or kill the session first" in response.json()["detail"]
        mock_task_manager.update.assert_not_called()

    def test_unmarked_second_assignee_tag_on_claimed_card_is_409(self, client, mock_task_manager):
        """Adding a second assignee tag alongside the existing one, via a
        bare tags-only PUT, must be refused on a claimed card even though
        `derive_assignee` (first-match-wins) still resolves to the same
        tag — comparing the normalized assignee-tag *set*, not the derived
        value, is what catches this."""
        self._set_current(mock_task_manager, tags=["claude", "agent-running"])
        response = client.put("/api/tasks/abc12345", json={"tags": ["claude", "agent-running", "codex"]})
        assert response.status_code == 409
        assert "answer or kill the session first" in response.json()["detail"]
        mock_task_manager.update.assert_not_called()

    def test_unmarked_second_assignee_tag_on_unclaimed_card_succeeds(self, client, mock_task_manager):
        """Positive case: the same second-assignee-tag patch on an
        UNCLAIMED agent card is still allowed — the set comparison only
        feeds into the existing claimed-only refusal, it doesn't add a new
        refusal of its own."""
        self._set_current(mock_task_manager, tags=["claude"])
        response = client.put("/api/tasks/abc12345", json={"tags": ["claude", "codex"]})
        assert response.status_code == 200
        mock_task_manager.update.assert_called_once()

    def test_unmarked_tags_patch_preserving_claim_and_assignee_on_claimed_card_succeeds(
        self, client, mock_task_manager,
    ):
        """Positive case: a tags-only PUT on a claimed card that keeps both
        the assignee tag and the claim tag exactly as they were — only
        adding an unrelated label — is not what this guard exists to
        catch, and must still succeed. The guard fires on a specific
        diff (a dropped claim tag, a changed assignee-tag set, or an
        added claim tag), not on the mere fact that the card is claimed."""
        self._set_current(mock_task_manager, tags=["codex", "agent-running"])
        response = client.put("/api/tasks/abc12345", json={
            "tags": ["codex", "agent-running", "needs-design"],
        })
        assert response.status_code == 200
        mock_task_manager.update.assert_called_once()

    def test_unmarked_tags_patch_adding_a_claim_tag_to_an_unclaimed_card_is_409(
        self, client, mock_task_manager,
    ):
        """The other half of the vector a bare tags PUT could otherwise
        reach: ADDING a claim tag (`agent-running`/`agent-blocked`) to a
        card that doesn't have one. No HTTP caller in this codebase adds a
        claim tag this way — the worker uses `/swap-tag` — so this is
        refused unconditionally, not only when the card was already
        claimed. Reproduces the exact "freeze a `me` card" vector: without
        this guard, an unmarked `{"tags": ["me", "agent-running"]}` PUT
        would succeed and every human move on that card would then read as
        refused on the very next policy read."""
        self._set_current(mock_task_manager, tags=["me"])
        response = client.put("/api/tasks/abc12345", json={"tags": ["me", "agent-running"]})
        assert response.status_code == 409
        assert "answer or kill the session first" in response.json()["detail"]
        mock_task_manager.update.assert_not_called()

    def test_unmarked_tags_patch_without_a_claim_tag_on_an_unclaimed_me_card_succeeds(
        self, client, mock_task_manager,
    ):
        """Positive case for the added-claim-tag guard: an ordinary,
        non-claim-tag tags edit on a `me` card is completely unaffected."""
        self._set_current(mock_task_manager, tags=["me"])
        response = client.put("/api/tasks/abc12345", json={"tags": ["me", "urgent"]})
        assert response.status_code == 200
        mock_task_manager.update.assert_called_once()

    def test_board_marker_missing_task_is_404(self, client, mock_task_manager):
        mock_task_manager.get.return_value = None
        response = client.put("/api/tasks/abc12345", json={
            "fields": {"effort": "high", "assigned_by": "board"},
        })
        assert response.status_code == 404
        mock_task_manager.update.assert_not_called()

    def test_unmarked_tags_patch_on_missing_task_is_404(self, client, mock_task_manager):
        """The tags guard's own task read 404s independently of the
        marker-gated fields read above."""
        mock_task_manager.get.return_value = None
        response = client.put("/api/tasks/abc12345", json={"tags": ["codex"]})
        assert response.status_code == 404
        mock_task_manager.update.assert_not_called()

    # --- the claim rule is keyed on a live session actually existing, not
    # on status alone (see api/services/agent_board.py's
    # `status_claim_possible` / `SessionStore.has_live_session`) ---

    def test_status_in_progress_agent_card_with_a_live_session_refuses_assignee_change(
        self, client, mock_task_manager,
    ):
        """An agent-owned card whose status merely reads "in_progress"
        (no `agent-running`/`agent-blocked` tag) is claimed only when a
        live session actually backs it — reproduced here with a real row
        in the (temp-dir, isolated) session store."""
        from api.routes import tasks as tasks_route
        current = self._set_current(mock_task_manager, status="in_progress", tags=["codex"])
        tasks_route._session_store.create(task_id=current.id, routing="claude_code")
        response = client.put("/api/tasks/abc12345", json={"tags": ["hermes"]})
        assert response.status_code == 409
        assert "answer or kill the session first" in response.json()["detail"]
        mock_task_manager.update.assert_not_called()

    def test_status_in_progress_agent_card_with_no_live_session_allows_assignee_change(
        self, client, mock_task_manager,
    ):
        """Positive case: the identical status/tags shape with NO session
        row behind it — the transition a board reassignment or a vault
        edit can produce — stays movable. (`_isolated_session_store`
        leaves the store empty by default, so this is really just proving
        the negative case still works with the new code path in place.)"""
        self._set_current(mock_task_manager, status="in_progress", tags=["codex"])
        response = client.put("/api/tasks/abc12345", json={"tags": ["hermes"]})
        assert response.status_code == 200
        mock_task_manager.update.assert_called_once()

    def test_no_fields_patch_never_reads_current_task(self, client, mock_task_manager):
        """No `fields` in the request at all -> the guard short-circuits
        before ever calling `manager.get` (no extra read on the hot path)."""
        response = client.put("/api/tasks/abc12345", json={"priority": "high"})
        assert response.status_code == 200
        mock_task_manager.get.assert_not_called()
        mock_task_manager.update.assert_called_once()

    # --- COMPLETE ---

    def test_complete_task(self, client, mock_task_manager):
        response = client.put("/api/tasks/abc12345/complete")
        assert response.status_code == 200
        mock_task_manager.complete.assert_called_once_with("abc12345")

    def test_complete_task_not_found(self, client, mock_task_manager):
        mock_task_manager.complete.return_value = None
        response = client.put("/api/tasks/nonexistent/complete")
        assert response.status_code == 404

    def test_complete_task_conflict_is_409(self, client, mock_task_manager):
        from api.services.task_manager import TaskConflictError
        mock_task_manager.complete.side_effect = TaskConflictError("too many conflicting writes")
        response = client.put("/api/tasks/abc12345/complete")
        assert response.status_code == 409

    # --- TAGS ---

    def test_list_tags(self, client, mock_task_manager):
        mock_task_manager.list_tags.return_value = [
            {"tag": "work", "count": 3},
            {"tag": "urgent", "count": 1},
        ]
        response = client.get("/api/tasks/tags")
        assert response.status_code == 200
        assert response.json() == {
            "tags": [
                {"tag": "work", "count": 3},
                {"tag": "urgent", "count": 1},
            ]
        }

    def test_list_tags_empty(self, client, mock_task_manager):
        mock_task_manager.list_tags.return_value = []
        response = client.get("/api/tasks/tags")
        assert response.status_code == 200
        assert response.json() == {"tags": []}

    # --- DELETE ---

    def test_delete_task(self, client, mock_task_manager):
        response = client.delete("/api/tasks/abc12345")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "deleted"
        assert data["id"] == "abc12345"

    def test_delete_task_not_found(self, client, mock_task_manager):
        mock_task_manager.delete.return_value = False
        response = client.delete("/api/tasks/nonexistent")
        assert response.status_code == 404

    def test_delete_task_conflict_is_409(self, client, mock_task_manager):
        from api.services.task_manager import TaskConflictError
        mock_task_manager.delete.side_effect = TaskConflictError("too many conflicting writes")
        response = client.delete("/api/tasks/abc12345")
        assert response.status_code == 409

    # --- RESPONSE SHAPE ---

    def test_task_response_shape(self, client, mock_task_manager):
        """Verify all expected fields are present in task response."""
        response = client.get("/api/tasks/abc12345")
        data = response.json()
        expected_fields = [
            "id", "description", "status", "context", "priority",
            "due_date", "created_date", "done_date", "cancelled_date",
            "updated_at", "tags", "reminder_id", "notes", "fields",
            "source_file", "line_number",
        ]
        for f in expected_fields:
            assert f in data, f"Missing field: {f}"

    # --- CONFLICTS ---

    def test_list_conflicts(self, client, mock_task_manager):
        mock_task_manager.list_conflicts.return_value = [
            {"name": "Inbox.sync-conflict-20260101-120000-ABCDEFG.md", "mtime": "2026-01-01T12:00:00+00:00"},
        ]
        response = client.get("/api/tasks/conflicts")
        assert response.status_code == 200
        data = response.json()
        assert len(data["conflicts"]) == 1
        assert data["conflicts"][0]["name"] == "Inbox.sync-conflict-20260101-120000-ABCDEFG.md"

    def test_list_conflicts_empty(self, client, mock_task_manager):
        mock_task_manager.list_conflicts.return_value = []
        response = client.get("/api/tasks/conflicts")
        assert response.status_code == 200
        assert response.json() == {"conflicts": []}

    def test_conflicts_route_not_captured_by_task_id(self, client, mock_task_manager):
        """'conflicts' must never be treated as a task id — regression guard
        for route registration order."""
        mock_task_manager.list_conflicts.return_value = []
        response = client.get("/api/tasks/conflicts")
        assert response.status_code == 200
        mock_task_manager.get.assert_not_called()

    # --- SWAP-TAG ---

    def test_swap_tag_conflict_is_409(self, client, mock_task_manager):
        from api.services.task_manager import TaskConflictError
        mock_task_manager.swap_tag.side_effect = TaskConflictError("too many conflicting writes")
        response = client.post("/api/tasks/abc12345/swap-tag?from=agent&to=agent-running")
        assert response.status_code == 409


class TestListTasksParameterDocs:
    """The MCP tool schema for `lifeos_task_list` is generated from this route's
    OpenAPI spec (mcp_server._build_input_schema). Bare `Optional[str] = None`
    params produce the placeholder "Query parameter: <name>", which tells a model
    nothing about valid values — so it guesses a context (0 rows) or omits status
    (every status, mostly done/cancelled) and reports a partial list as complete.
    """

    @pytest.fixture
    def params(self):
        from api.main import app
        spec = app.openapi()["paths"]["/api/tasks"]["get"]["parameters"]
        return {p["name"]: p.get("description", "") for p in spec}

    def test_no_param_falls_back_to_placeholder_description(self, params):
        assert set(params) == {"status", "context", "tag", "due_before", "query"}
        for name, desc in params.items():
            assert desc, f"{name} has no description; MCP would emit a placeholder"
            assert "Query parameter:" not in desc

    def test_status_description_enumerates_valid_values(self, params):
        desc = params["status"]
        for value in ("todo", "done", "in_progress", "cancelled",
                      "deferred", "blocked", "urgent"):
            assert value in desc
        # The failure mode was omitting status entirely and summarising everything.
        assert "todo" in desc and "omit" in desc.lower()

    def test_context_description_warns_unused_values_return_nothing(self, params):
        desc = params["context"].lower()
        assert "zero" in desc or "no tasks" in desc
        assert "inbox" in desc

    def test_fallback_schema_does_not_invent_context_values(self):
        """The offline fallback must not advertise contexts that no vault has."""
        import mcp_server
        schema = mcp_server.LifeOSMCPServer._fallback_schemas(
            mcp_server.LifeOSMCPServer.__new__(mcp_server.LifeOSMCPServer)
        )["lifeos_task_list"]
        context_desc = schema["properties"]["context"]["description"]
        assert "Work, Personal" not in context_desc
        assert "Inbox" in context_desc
