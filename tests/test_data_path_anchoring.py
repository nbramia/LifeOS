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

#1038 covers the same bug class for ``config.settings.Settings``'s
``vault_path``/``chroma_path`` defaults: nineteen call sites derive a
data-store path from ``Path(settings.chroma_path).parent`` at call time, so
anchoring the *default* (rather than each derived helper) fixes all of them
at once. ``get_crm_db_path()``, ``get_conversation_db_path()``, and
``get_bm25_db_path()`` below are a representative sample of those derived
helpers, exercised the same foreign-cwd way as the #645 cases above.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_without_data_path_overrides() -> dict:
    """A copy of the current environment with `LIFEOS_CHROMA_PATH` and
    `LIFEOS_VAULT_PATH` removed.

    These tests are about the *default* `settings.chroma_path`/`vault_path`
    resolve to, so the subprocess must not inherit an operator (or test
    runner) override of either — `scripts/verify_candidate.py` sets both to
    a runtime-scoped path on every lane run, which would otherwise make
    every case here resolve against that runtime root instead of the repo
    root and fail for the wrong reason.
    """
    env = os.environ.copy()
    env.pop("LIFEOS_CHROMA_PATH", None)
    env.pop("LIFEOS_VAULT_PATH", None)
    return env


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
        env=_env_without_data_path_overrides(),
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
        (
            "from api.utils.db_paths import get_crm_db_path",
            "get_crm_db_path()",
            "data/crm.db",
        ),
        (
            "from api.services.conversation_store import get_conversation_db_path",
            "get_conversation_db_path()",
            "data/conversations.db",
        ),
        (
            "from api.services.bm25_index import get_bm25_db_path",
            "get_bm25_db_path()",
            "data/bm25_index.db",
        ),
    ],
    ids=[
        "slack_integration.SLACK_TOKEN_PATH",
        "scheduler_store.DEFAULT_INDEX_PATH",
        "task_manager.DEFAULT_INDEX_PATH",
        "telegram.TelegramBotListener._STATE_FILE",
        "person_stats' PersonEntityStore.CRM_DB_PATH",
        "cc_wezterm_store.DEFAULT_DB_PATH",
        "db_paths.get_crm_db_path()",
        "conversation_store.get_conversation_db_path()",
        "bm25_index.get_bm25_db_path()",
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


@pytest.mark.unit
def test_usage_store_db_path_resolves_to_repo_root_from_foreign_cwd(tmp_path):
    """`UsageStore.__init__` computes `db_path` from `settings.chroma_path`
    and, as a side effect, creates the sqlite file with a real schema — so
    avoid instantiating against the real default in-process (that would
    touch this repo's actual data/usage.db). As with the slack indexer case
    above, patch `_init_db` to a no-op inside the subprocess before
    constructing, so only the path computation is observed."""
    foreign_cwd = tmp_path / "not-the-repo"
    foreign_cwd.mkdir()

    code = (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from unittest.mock import patch\n"
        "from api.services.usage_store import UsageStore\n"
        "with patch.object(UsageStore, '_init_db', lambda self: None):\n"
        "    store = UsageStore()\n"
        "print(store.db_path)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(foreign_cwd),
        capture_output=True,
        text=True,
        timeout=30,
        env=_env_without_data_path_overrides(),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(REPO_ROOT / "data" / "usage.db")
    assert not (foreign_cwd / "data").exists()


def _run_get_crm_db_path(foreign_cwd: Path, chroma_path_env: str) -> str:
    code = (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from api.utils.db_paths import get_crm_db_path\n"
        "print(get_crm_db_path())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(foreign_cwd),
        capture_output=True,
        text=True,
        timeout=30,
        env={**_env_without_data_path_overrides(), "LIFEOS_CHROMA_PATH": chroma_path_env},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.unit
def test_explicit_relative_chroma_path_still_resolves_against_cwd_from_foreign_cwd(
    tmp_path,
):
    """An operator-set, deliberately relative `LIFEOS_CHROMA_PATH` must still
    resolve against cwd exactly as before — anchoring only the *default*
    must not change resolution for a caller that configures its own path.
    `get_crm_db_path()` derives from `Path(settings.chroma_path).parent`, so
    a relative override of `data/explicit_chroma` yields a `crm.db` sibling
    under `data/`. The returned string stays relative (unchanged from
    pre-anchoring behavior), but the directory it creates as a side effect
    is resolved against the foreign cwd, not the repo root."""
    foreign_cwd = tmp_path / "not-the-repo"
    foreign_cwd.mkdir()

    resolved = _run_get_crm_db_path(foreign_cwd, "data/explicit_chroma")

    assert resolved == "data/crm.db"
    assert (foreign_cwd / "data").is_dir()
    assert not (REPO_ROOT / "data" / "explicit_chroma").exists()


@pytest.mark.unit
def test_explicit_absolute_chroma_path_with_space_is_preserved(tmp_path):
    """A real-world absolute override may contain a space (e.g. a vault
    path under a directory named with a date/year). Anchoring the default
    must not corrupt an explicit override reached through string handling
    that assumes no whitespace."""
    foreign_cwd = tmp_path / "not-the-repo"
    foreign_cwd.mkdir()
    chroma_dir = tmp_path / "My Chroma 2099" / "chromadb"

    resolved = _run_get_crm_db_path(foreign_cwd, str(chroma_dir))

    assert resolved == str(tmp_path / "My Chroma 2099" / "crm.db")
