"""Regression test for #1012: ``get_task_manager()``'s default singleton
writes into the real checkout.

``TaskManager.__init__`` unconditionally loads/creates its index file and
rewrites ``vault/LifeOS/Tasks/Dashboard.md`` the moment it constructs -- there
is no lazy "only touch disk when asked" path. The only call site that
constructs one with every argument defaulted is ``get_task_manager()``
itself, reached without an explicit override by
``agent_system_prompt._get_existing_tags()`` while building a chat system
prompt -- exactly the path ``test_agent_loop_error_messages.py``'s
``run_agent_loop(...)`` calls exercise. Absent isolation, a plain local
pytest run (not routed through ``verify_candidate.py``'s candidate-data
symlink) creates ``data/task_index.json`` and
``vault/LifeOS/Tasks/Dashboard.md`` in the real checkout the moment any test
reaches this path.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
_REAL_TASK_INDEX = REPO_ROOT / "data" / "task_index.json"
_REAL_DASHBOARD = REPO_ROOT / "vault" / "LifeOS" / "Tasks" / "Dashboard.md"


def test_default_task_manager_paths_are_isolated_from_the_real_checkout(tmp_path_factory):
    """``get_task_manager()``'s default singleton must resolve its vault and
    index paths away from the real checkout, regardless of whether
    ``data/task_index.json``/``vault/LifeOS/Tasks/Dashboard.md`` happen to
    already exist there. An existence check alone (see the sibling test
    below) has no failure mode on a checkout where those files are already
    present -- this pins the actual ownership property instead."""
    from config.settings import Settings
    from api.services.task_manager import get_task_manager

    real_default_vault_path = Path(Settings().vault_path)
    real_default_index_path = REPO_ROOT / "data" / "task_index.json"

    manager = get_task_manager()

    assert manager.vault_path != real_default_vault_path
    assert manager.index_path != real_default_index_path
    basetemp = tmp_path_factory.getbasetemp()
    assert manager.vault_path.is_relative_to(basetemp)
    assert manager.index_path.is_relative_to(basetemp)


def test_building_existing_tags_block_does_not_touch_the_real_checkout():
    """Exercising the same call path ``run_agent_loop`` uses to build a
    system prompt must not create task-store files in the real checkout.
    Adds value on a checkout that starts clean; see the state-independent
    test above for the property that holds regardless of ambient state."""
    from api.services import agent_system_prompt

    index_existed_before = _REAL_TASK_INDEX.exists()
    dashboard_existed_before = _REAL_DASHBOARD.exists()

    agent_system_prompt._get_existing_tags()

    assert _REAL_TASK_INDEX.exists() == index_existed_before
    assert _REAL_DASHBOARD.exists() == dashboard_existed_before
