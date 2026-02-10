"""
Resolve working directory for Claude Code tasks based on task description keywords.
"""
import os
import re
from pathlib import Path

# Multi-word phrases first (checked as substrings), then single words (checked with word boundaries)
_VAULT_PHRASES = ["meeting notes", "daily note"]
_VAULT_WORDS = ["note", "notes", "vault", "obsidian", "journal", "backlog"]

_LIFEOS_PHRASES = ["life os", "api endpoint", "telegram bot"]
_LIFEOS_WORDS = [
    "lifeos", "server", "sync", "chromadb",
    "readme", "test", "tests", "deploy", "endpoint", "route",
    "bug", "fix", "config", "backup", "database", "db",
    "api", "search", "health", "telegram",
]

_CODE_WORDS = ["script", "code", "function", "cron"]

_HOME = os.path.expanduser("~")
_VAULT_DIR = os.path.join(_HOME, "Notes 2025")
_LIFEOS_DIR = os.path.join(_HOME, "Documents", "Code", "LifeOS")
_CODE_DIR = os.path.join(_HOME, "Documents", "Code")

# Cache for scanned project directories
_project_dirs: list[tuple[str, str]] | None = None


def _scan_projects() -> list[tuple[str, str]]:
    """Scan ~/Documents/Code/ for project directories. Returns (name_lower, full_path) sorted longest-name-first."""
    global _project_dirs
    if _project_dirs is not None:
        return _project_dirs

    projects = []
    code_path = Path(_CODE_DIR)
    if code_path.is_dir():
        for entry in code_path.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                projects.append((entry.name.lower(), str(entry)))

    # Sort by name length descending so longest match wins
    projects.sort(key=lambda p: len(p[0]), reverse=True)
    _project_dirs = projects
    return _project_dirs


def resolve_working_directory(task: str) -> str:
    """Map a task description to the most appropriate working directory."""
    task_lower = task.lower()

    # 1. Vault/notes keywords
    for phrase in _VAULT_PHRASES:
        if phrase in task_lower:
            return _VAULT_DIR
    for word in _VAULT_WORDS:
        if re.search(rf"\b{word}\b", task_lower):
            return _VAULT_DIR

    # 2. LifeOS-specific keywords
    for phrase in _LIFEOS_PHRASES:
        if phrase in task_lower:
            return _LIFEOS_DIR
    for word in _LIFEOS_WORDS:
        if re.search(rf"\b{word}\b", task_lower):
            return _LIFEOS_DIR

    # 3. Scan project directories for name match
    for name, path in _scan_projects():
        if name in task_lower:
            return path

    # 4. General code keywords
    for word in _CODE_WORDS:
        if re.search(rf"\b{word}\b", task_lower):
            return _CODE_DIR

    # 5. Default to home
    return _HOME
