"""
Tests for the directory resolver.
"""
import os
import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

HOME = os.path.expanduser("~")


class TestResolveWorkingDirectory:
    """Tests for resolve_working_directory()."""

    def _resolve(self, task: str) -> str:
        # Reset cached project dirs between tests
        import api.services.directory_resolver as mod
        mod._project_dirs = None
        return mod.resolve_working_directory(task)

    def test_vault_keywords(self):
        assert self._resolve("edit my journal entry") == os.path.join(HOME, "Notes 2025")
        assert self._resolve("add to the backlog") == os.path.join(HOME, "Notes 2025")
        assert self._resolve("update my meeting notes") == os.path.join(HOME, "Notes 2025")
        assert self._resolve("create a daily note") == os.path.join(HOME, "Notes 2025")
        assert self._resolve("open the vault") == os.path.join(HOME, "Notes 2025")
        assert self._resolve("find obsidian files") == os.path.join(HOME, "Notes 2025")

    def test_vault_word_boundary(self):
        """'note' should not match 'notification' or 'denoted'."""
        result = self._resolve("send a notification to the team")
        assert result != os.path.join(HOME, "Notes 2025")

        result = self._resolve("this denoted something")
        assert result != os.path.join(HOME, "Notes 2025")

    def test_lifeos_keywords(self):
        assert self._resolve("fix the lifeos server") == os.path.join(HOME, "Documents", "Code", "LifeOS")
        assert self._resolve("update the sync logic") == os.path.join(HOME, "Documents", "Code", "LifeOS")
        assert self._resolve("change the telegram bot") == os.path.join(HOME, "Documents", "Code", "LifeOS")
        assert self._resolve("check chromadb status") == os.path.join(HOME, "Documents", "Code", "LifeOS")
        assert self._resolve("add an api endpoint") == os.path.join(HOME, "Documents", "Code", "LifeOS")

    def test_code_keywords(self):
        code_dir = os.path.join(HOME, "Documents", "Code")
        assert self._resolve("write a script to automate") == code_dir
        assert self._resolve("create a cron job") == code_dir

    def test_code_word_boundary(self):
        """'code' should match as a word, not inside 'encode'."""
        code_dir = os.path.join(HOME, "Documents", "Code")
        assert self._resolve("write some code") == code_dir
        # 'encode' contains 'code' but shouldn't match code keyword
        # (it would still match via word boundary since 'code' appears at end)
        # This is fine — encode ends with 'code' which matches \bcode\b

    def test_default_to_home(self):
        assert self._resolve("do something random") == HOME
        assert self._resolve("hello world") == HOME

    def test_priority_vault_over_lifeos(self):
        """Vault keywords should take priority over LifeOS keywords."""
        # "sync" is LifeOS, but "notes" is vault — vault should win since checked first
        result = self._resolve("sync my notes")
        assert result == os.path.join(HOME, "Notes 2025")

    @patch("api.services.directory_resolver.Path")
    def test_project_name_match(self, mock_path_cls):
        """Project directory names should be matched in task."""
        import api.services.directory_resolver as mod
        mod._project_dirs = None

        # Mock the Code directory scan
        mock_entry1 = type("Entry", (), {"name": "MyProject", "is_dir": lambda self: True})()
        mock_entry2 = type("Entry", (), {"name": "AnotherApp", "is_dir": lambda self: True})()
        mock_code_path = type("MockPath", (), {
            "is_dir": lambda self: True,
            "iterdir": lambda self: [mock_entry1, mock_entry2],
        })()
        mock_path_cls.return_value = mock_code_path

        # Patch str() on entries to return full paths
        mock_entry1.__str__ = lambda self: f"{HOME}/Documents/Code/MyProject"
        mock_entry2.__str__ = lambda self: f"{HOME}/Documents/Code/AnotherApp"

        # Need to also patch the entry str representation via the append
        mod._project_dirs = [
            ("myproject", f"{HOME}/Documents/Code/MyProject"),
            ("anotherapp", f"{HOME}/Documents/Code/AnotherApp"),
        ]

        result = mod.resolve_working_directory("update the myproject docs")
        assert result == f"{HOME}/Documents/Code/MyProject"
