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


def test_building_existing_tags_block_does_not_touch_the_real_checkout():
    """Exercising the same call path ``run_agent_loop`` uses to build a
    system prompt must not create task-store files in the real checkout."""
    from api.services import agent_system_prompt

    index_existed_before = _REAL_TASK_INDEX.exists()
    dashboard_existed_before = _REAL_DASHBOARD.exists()

    agent_system_prompt._get_existing_tags()

    assert _REAL_TASK_INDEX.exists() == index_existed_before
    assert _REAL_DASHBOARD.exists() == dashboard_existed_before
