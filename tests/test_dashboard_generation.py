"""
Tests for the dynamic Dashboard.md generator in TaskManager.
"""
import pytest

from api.services.task_manager import TaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def tm(tmp_path):
    return TaskManager(
        vault_path=tmp_path / "vault",
        index_path=tmp_path / "task_index.json",
    )


def _dashboard_text(tm):
    return (tm.tasks_dir / "Dashboard.md").read_text(encoding="utf-8")


class TestDashboardStructure:
    def test_dashboard_has_all_standard_sections(self, tm):
        text = _dashboard_text(tm)
        for heading in (
            "## Due This Week",
            "## Urgent",
            "## In Progress",
            "## Stale — open 30+ days",
            "## By Tag",
            "## All Open",
            "## Completed",
        ):
            assert heading in text, f"Missing section: {heading}"

    def test_removed_sections_are_gone(self, tm):
        text = _dashboard_text(tm)
        assert "## Blocked" not in text
        assert "## By Priority" not in text
        # The renamed section uses 'Completed' (not 'Recently Completed')
        assert "## Recently Completed" not in text

    def test_all_open_sorts_by_created_reverse(self, tm):
        text = _dashboard_text(tm)
        # Extract the All Open block
        block_start = text.index("## All Open")
        block = text[block_start:block_start + 400]
        assert "sort by created reverse" in block

    def test_due_this_week_sorts_by_due_ascending(self, tm):
        text = _dashboard_text(tm)
        start = text.index("## Due This Week")
        end = text.index("##", start + 5)
        block = text[start:end]
        # 'sort by due' (no 'reverse') = soonest first
        assert "sort by due" in block
        assert "sort by due reverse" not in block

    def test_completed_sorts_by_done_reverse(self, tm):
        text = _dashboard_text(tm)
        start = text.index("## Completed")
        block = text[start:]
        # Most-recently-completed first
        assert "sort by done reverse" in block
        # No artificial 7-day window — show all completed tasks
        assert "done after" not in block

    def test_urgent_section_uses_status_filter(self, tm):
        text = _dashboard_text(tm)
        start = text.index("## Urgent")
        end = text.index("##", start + 5)
        block = text[start:end]
        assert "status.name includes Urgent" in block

    def test_auto_generated_marker(self, tm):
        text = _dashboard_text(tm)
        assert "AUTO-GENERATED" in text


class TestStatsHeader:
    def test_counts_reflect_open_tasks(self, tm):
        tm.create("a")
        tm.create("b")
        tm.create("c")
        text = _dashboard_text(tm)
        assert "**3 open**" in text

    def test_overdue_count(self, tm):
        tm.create("late", due_date="2000-01-01")
        tm.create("ok")
        text = _dashboard_text(tm)
        assert "1 overdue" in text

    def test_done_within_7_days_counted(self, tm):
        t = tm.create("did it")
        tm.update(t.id, status="done")
        text = _dashboard_text(tm)
        # 0 open + 1 done in last 7 days
        assert "**0 open**" in text
        assert "1 done in last 7 days" in text


class TestOverdueSection:
    def test_overdue_section_appears_only_when_overdue_exists(self, tm):
        # No overdue tasks initially
        text = _dashboard_text(tm)
        assert "## Overdue" not in text

        tm.create("late one", due_date="2000-01-01")
        text = _dashboard_text(tm)
        assert "## Overdue" in text


class TestTagSections:
    def test_no_tag_sections_when_no_tagged_tasks(self, tm):
        tm.create("plain task")
        text = _dashboard_text(tm)
        # Untagged → "No tag" section appears
        assert "### No tag" in text
        # No specific tag sections
        assert "tag includes" not in text

    def test_tag_section_per_distinct_open_tag(self, tm):
        tm.create("a", tags=["work"])
        tm.create("b", tags=["urgent", "work"])
        text = _dashboard_text(tm)
        assert "### #work" in text
        assert "### #urgent" in text
        assert "tag includes #work" in text
        assert "tag includes #urgent" in text

    def test_no_tag_section_when_some_open_tasks_untagged(self, tm):
        tm.create("plain")
        tm.create("with tag", tags=["x"])
        text = _dashboard_text(tm)
        assert "### No tag" in text
        assert "no tags" in text

    def test_no_tag_section_skipped_when_all_tagged(self, tm):
        tm.create("with tag", tags=["x"])
        text = _dashboard_text(tm)
        assert "### No tag" not in text

    def test_tag_section_skipped_for_done_only_tag(self, tm):
        """Tags appear in the dashboard only if at least one OPEN task has them."""
        t = tm.create("done one", tags=["archived"])
        tm.update(t.id, status="done")
        text = _dashboard_text(tm)
        assert "### #archived" not in text

    def test_tag_sections_sorted_by_count_desc(self, tm):
        tm.create("a", tags=["work"])
        tm.create("b", tags=["work"])
        tm.create("c", tags=["work"])
        tm.create("d", tags=["urgent"])
        text = _dashboard_text(tm)
        # work appears 3x, urgent 1x — work's heading should appear first
        assert text.index("### #work") < text.index("### #urgent")


class TestRegenerationTriggers:
    def test_create_regenerates_dashboard(self, tm):
        before = _dashboard_text(tm)
        tm.create("new thing", tags=["fresh"])
        after = _dashboard_text(tm)
        assert "### #fresh" not in before
        assert "### #fresh" in after

    def test_delete_regenerates_dashboard(self, tm):
        task = tm.create("temp", tags=["temp-tag"])
        assert "### #temp-tag" in _dashboard_text(tm)
        tm.delete(task.id)
        assert "### #temp-tag" not in _dashboard_text(tm)

    def test_update_regenerates_dashboard(self, tm):
        task = tm.create("a", tags=["original"])
        tm.update(task.id, tags=["renamed"])
        text = _dashboard_text(tm)
        assert "### #renamed" in text
        assert "### #original" not in text

    def test_reindex_skips_dashboard_file(self, tm):
        """Reindexing Dashboard.md should be a no-op (no watcher feedback loop)."""
        tm.create("a")
        dashboard_path = tm.tasks_dir / "Dashboard.md"
        # Should not raise, should not add fake tasks
        tm.reindex_file(str(dashboard_path))
        assert len(tm.list_tasks()) == 1


class TestDashboardIdempotence:
    def test_repeated_writes_with_no_change_skip_redundant_io(self, tm, monkeypatch):
        """Writing the same content twice should not re-touch the file."""
        # Build once
        tm._write_dashboard()
        path = tm.tasks_dir / "Dashboard.md"
        # Force a fixed timestamp so the content is byte-identical between calls
        import api.services.task_manager as tm_mod

        class _FrozenDateTime:
            @staticmethod
            def now():
                from datetime import datetime
                return datetime(2026, 1, 1, 12, 0, 0)

        monkeypatch.setattr(tm_mod, "datetime", _FrozenDateTime)
        tm._write_dashboard()
        first_mtime = path.stat().st_mtime
        tm._write_dashboard()
        second_mtime = path.stat().st_mtime
        assert first_mtime == second_mtime
