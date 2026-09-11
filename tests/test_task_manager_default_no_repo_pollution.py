"""``get_task_manager()``'s default singleton resolves isolated vault and
index paths.

``TaskManager.__init__`` loads/creates its index file and rewrites
``vault/LifeOS/Tasks/Dashboard.md`` unconditionally, and ``get_task_manager()``
is the only call site that constructs one with every argument defaulted --
reachable, for example, from ``agent_system_prompt._get_existing_tags()``
while building a chat system prompt.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
_REAL_TASK_INDEX = REPO_ROOT / "data" / "task_index.json"
_REAL_DASHBOARD = REPO_ROOT / "vault" / "LifeOS" / "Tasks" / "Dashboard.md"


def test_default_task_manager_paths_are_isolated_from_the_real_checkout(tmp_path_factory):
    """The default ``TaskManager()`` reached via ``get_task_manager()`` must
    resolve its vault and index paths under this test's own pytest-managed
    temp tree, regardless of whether ``data/task_index.json`` or
    ``vault/LifeOS/Tasks/Dashboard.md`` happen to already exist in the real
    checkout -- an existence check alone (see the sibling test below) has no
    failure mode when those files are already present."""
    from config.settings import Settings
    from api.services.task_manager import get_task_manager

    real_default_vault_path = Path(Settings().vault_path)

    manager = get_task_manager()

    assert manager.vault_path != real_default_vault_path
    assert manager.index_path != _REAL_TASK_INDEX
    basetemp = tmp_path_factory.getbasetemp()
    assert manager.vault_path.is_relative_to(basetemp)
    assert manager.index_path.is_relative_to(basetemp)


def test_building_existing_tags_block_does_not_touch_the_real_checkout():
    """Exercising the same call path ``run_agent_loop`` uses to build a
    system prompt must not create task-store files in the real checkout.
    This is a secondary check with a failure mode only on a checkout that
    starts clean; the test above pins the property that holds regardless of
    ambient state."""
    from api.services import agent_system_prompt

    index_existed_before = _REAL_TASK_INDEX.exists()
    dashboard_existed_before = _REAL_DASHBOARD.exists()

    agent_system_prompt._get_existing_tags()

    assert _REAL_TASK_INDEX.exists() == index_existed_before
    assert _REAL_DASHBOARD.exists() == dashboard_existed_before
