"""config.settings imports in the MCP bridge venv, which has no python-frontmatter (nbramia/personas#63)."""
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent


def test_settings_and_task_manager_import_without_frontmatter():
    code = (
        "import sys; sys.modules['frontmatter'] = None\n"
        "from config.settings import settings\n"
        "from api.services.task_manager import CONTEXT_RE\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-800:]
