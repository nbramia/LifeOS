"""Regression tests for #645: seven data-store defaults resolved against the
*process's* working directory instead of the repo root — the same class of
bug ``mcp_server.py`` already had to defend against for the agent-session
stores (see ``test_inter_agent_stores_anchored_to_repo_not_cwd``).

Every existing test in this suite runs from the repo root, so none of them
can see a cwd-relative default resolving incorrectly — the module is always
imported (and its constants computed) with cwd == repo root. To actually
exercise the bug, these tests spawn a fresh subprocess with its *working
directory* set to somewhere outside the repo before the module is ever
imported, mirroring how a non-repo-root caller (e.g. a stdio MCP child) sees
these modules. That also sidesteps this suite's autouse isolation fixtures
(e.g. ``_isolate_telegram_state_file``), which would otherwise mask the
in-process class attribute under a fixture-chosen tmp path.
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_in_foreign_cwd(foreign_cwd: Path, import_stmt: str, expr: str) -> str:
    """Run `expr` in a fresh subprocess whose cwd is `foreign_cwd` and whose
    sys.path is seeded with the repo root, and return its printed result."""
    code = f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); {import_stmt}; print({expr})"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(foreign_cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.unit
@pytest.mark.parametrize(
    "import_stmt, expr, relative_data_file",
    [
        (
            "from api.services.slack_integration import SLACK_TOKEN_PATH",
            "SLACK_TOKEN_PATH",
            "data/slack_tokens.json",
        ),
        (
            "from api.services.scheduler_store import DEFAULT_INDEX_PATH",
            "DEFAULT_INDEX_PATH",
            "data/scheduler_index.json",
        ),
        (
            "from api.services.task_manager import DEFAULT_INDEX_PATH",
            "DEFAULT_INDEX_PATH",
            "data/task_index.json",
        ),
        (
            "from api.services.telegram import TelegramBotListener",
            "TelegramBotListener._STATE_FILE",
            "data/telegram_state.json",
        ),
        (
            "from api.services.person_stats import PersonEntityStore",
            "PersonEntityStore.CRM_DB_PATH",
            "data/crm.db",
        ),
        (
            "from api.services.cc_wezterm_store import DEFAULT_DB_PATH",
            "DEFAULT_DB_PATH",
            "data/cc_wezterm.db",
        ),
    ],
    ids=[
        "slack_integration.SLACK_TOKEN_PATH",
        "scheduler_store.DEFAULT_INDEX_PATH",
        "task_manager.DEFAULT_INDEX_PATH",
        "telegram.TelegramBotListener._STATE_FILE",
        "person_stats' PersonEntityStore.CRM_DB_PATH",
        "cc_wezterm_store.DEFAULT_DB_PATH",
    ],
)
def test_default_resolves_to_repo_root_from_foreign_cwd(
    tmp_path, import_stmt, expr, relative_data_file
):
    foreign_cwd = tmp_path / "not-the-repo"
    foreign_cwd.mkdir()

    resolved = _resolve_in_foreign_cwd(foreign_cwd, import_stmt, expr)

    assert resolved == str(REPO_ROOT / relative_data_file)
    # No phantom `data/` directory should appear under the foreign cwd.
    assert not (foreign_cwd / "data").exists()


@pytest.mark.unit
def test_slack_indexer_ts_db_path_resolves_to_repo_root_from_foreign_cwd(tmp_path):
    """`SlackIndexer._ts_db_path` is computed in `__init__`, not a module
    constant, and `__init__` also creates the sqlite file as a side effect —
    so avoid instantiating against the real default in-process (that would
    touch this repo's actual data/slack_sync_timestamps.db). Instead, patch
    `_init_timestamp_db` to a no-op inside the subprocess before constructing,
    so only the path computation is observed."""
    foreign_cwd = tmp_path / "not-the-repo"
    foreign_cwd.mkdir()

    code = (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from unittest.mock import patch\n"
        "from api.services.slack_indexer import SlackIndexer\n"
        "with patch.object(SlackIndexer, '_init_timestamp_db', lambda self: None):\n"
        "    idx = SlackIndexer()\n"
        "print(idx._ts_db_path)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(foreign_cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(REPO_ROOT / "data" / "slack_sync_timestamps.db")
    assert not (foreign_cwd / "data").exists()


@pytest.mark.unit
def test_slack_token_store_explicit_relative_path_still_resolves_against_cwd(
    tmp_path, monkeypatch
):
    """Anchoring the *default* must not change resolution for a caller that
    deliberately passes a relative path — it should still resolve against
    cwd exactly as before. Credentials path, so covered explicitly per #645."""
    from api.services.slack_integration import SlackTokenStore

    monkeypatch.chdir(tmp_path)
    store = SlackTokenStore(path=Path("data/explicit_tokens.json"))
    assert store.path == Path("data/explicit_tokens.json")
    assert not store.path.is_absolute()


@pytest.mark.unit
def test_cc_wezterm_store_explicit_relative_path_still_resolves_against_cwd(
    tmp_path, monkeypatch
):
    from api.services.cc_wezterm_store import CCWezTermStore

    monkeypatch.chdir(tmp_path)
    store = CCWezTermStore(db_path=Path("data/explicit_wezterm.db"))
    assert store.db_path == Path("data/explicit_wezterm.db")
    # The relative path resolved against the (foreign) cwd, not the repo root.
    assert (tmp_path / "data" / "explicit_wezterm.db").exists()


@pytest.mark.unit
def test_task_manager_explicit_relative_index_path_still_resolves_against_cwd(
    tmp_path, monkeypatch
):
    from api.services.task_manager import TaskManager

    monkeypatch.chdir(tmp_path)
    manager = TaskManager(
        vault_path=tmp_path / "vault", index_path=Path("data/explicit_tasks.json")
    )
    assert manager.index_path == Path("data/explicit_tasks.json")
    assert (tmp_path / "data" / "explicit_tasks.json").parent.exists()


@pytest.mark.unit
def test_scheduler_store_explicit_relative_index_path_still_resolves_against_cwd(
    tmp_path, monkeypatch
):
    from api.services.scheduler_store import SchedulerStore

    monkeypatch.chdir(tmp_path)
    store = SchedulerStore(
        vault_path=tmp_path / "vault", index_path=Path("data/explicit_sched.json")
    )
    assert store.index_path == Path("data/explicit_sched.json")
    assert (tmp_path / "data" / "explicit_sched.json").parent.exists()
