"""
Resolve working directory for Claude Code tasks. Prefers a Jev fan-out
judgment (`jev_task_routing.judge_task`) when configured and confident;
falls back to a keyword guess off the task description otherwise.
"""
import contextlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from config.settings import settings

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
_CODE_DIR = os.path.expanduser(settings.code_dir)
_LIFEOS_DIR = os.path.join(_CODE_DIR, "LifeOS")

# Cache for scanned project directories
_project_dirs: list[tuple[str, str]] | None = None


def _scan_projects() -> list[tuple[str, str]]:
    """Scan code directory for project directories. Returns (name_lower, full_path) sorted longest-name-first."""
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


_GITHUB_CACHE_TTL_SECONDS = 24 * 60 * 60
_GH_TIMEOUT_SECONDS = 10
_GH_CLONE_TIMEOUT_SECONDS = 120

# GitHub repo names are `[A-Za-z0-9._-]`, 1-100 chars — but that charset
# alone still matches `.`/`..`, which would otherwise resolve to `code_dir`
# itself or its parent. Every repo name (freshly fetched from `gh` OR read
# back from the on-disk cache) is validated against this before it's
# trusted for a path — the cache is a file on disk, not something this
# process fully controls the contents of.
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def _valid_repo_name(name: object) -> bool:
    return isinstance(name, str) and bool(_REPO_NAME_RE.match(name)) and name not in (".", "..")


def _code_root() -> Path:
    return Path(settings.code_dir).expanduser().resolve()


def _repo_path(name: str) -> str:
    """Where `name` would live under `code_dir`. Never derived from
    anything but a validated name — a cached or gh-reported `path` is
    never trusted directly (see `_valid_repo_name`)."""
    return str(_code_root() / name)


def _github_cache_path() -> Path:
    """On-disk cache for the GitHub repo listing and resolved owner login,
    under the same data directory other per-process caches use (falls back
    to a local `data/` dir when `settings.chroma_path` can't be read)."""
    try:
        data_dir = Path(settings.chroma_path).parent
    except Exception:
        data_dir = Path("data")
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "github_repos_cache.json"


def _read_github_cache_raw(cache_file: Path) -> dict:
    try:
        raw = json.loads(cache_file.read_text())
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _read_github_repo_cache(cache_file: Path) -> list[tuple[str, str, str]] | None:
    """Cached repos if the cache exists and is under 24h old, else None.
    Each entry's `path` is rebuilt from its (validated) name — never
    deserialized from the cache file itself."""
    raw = _read_github_cache_raw(cache_file)
    fetched_at = raw.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    if time.time() - fetched_at >= _GITHUB_CACHE_TTL_SECONDS:
        return None
    repos = raw.get("repos")
    if not isinstance(repos, list):
        return None
    result = []
    for entry in repos:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not _valid_repo_name(name):
            continue
        description = entry.get("description") or "local project directory"
        result.append((name, description, _repo_path(name)))
    return result


def _write_github_cache(cache_file: Path, owner: str, repos: list[tuple[str, str, str]]) -> None:
    """Writes via a temp file + `os.replace` in the same directory, so a
    concurrent reader never observes a partially-written cache file — it
    either sees the prior complete version or the new one, never a
    truncated/interleaved one."""
    payload = json.dumps({
        "owner": owner,
        "fetched_at": time.time(),
        "repos": [{"name": n, "description": d, "path": p} for n, d, p in repos],
    })
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(cache_file.parent), prefix=".github_repos_cache.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            os.replace(tmp_path, cache_file)
        except Exception:
            with contextlib.suppress(OSError):
                os.remove(tmp_path)
            raise
    except Exception:
        pass  # Caching is an optimization; a write failure must not break resolution.


def _run_gh(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    """The single seam every real `gh` subprocess call in this module goes
    through — `_resolve_github_owner`, `_github_repos`, and `ensure_cloned`
    all call this instead of `subprocess.run` directly, so a test
    hermeticity guard (see `tests/conftest.py`) has exactly one place to
    intercept."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _resolve_github_owner(cache_file: Path) -> str:
    """`settings.github_owner` when set; else the cached login from a prior
    `gh api user` call; else a fresh `gh api user -q .login`. Empty string
    (never raises) when none of those produce a login."""
    if settings.github_owner:
        return settings.github_owner
    cached_owner = _read_github_cache_raw(cache_file).get("owner")
    if cached_owner:
        return str(cached_owner)
    try:
        result = _run_gh(["gh", "api", "user", "-q", ".login"], timeout=_GH_TIMEOUT_SECONDS)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _github_repos() -> list[tuple[str, str, str]]:
    """The operator's GitHub repositories as (name, description, path),
    `path` being where the repo would live under `_CODE_DIR` whether or not
    it's cloned there yet. Cached on disk for 24 hours. Any failure —
    `gh` missing, no resolvable owner, a non-zero exit, malformed JSON —
    returns an empty list rather than raising, so the location option set
    just degrades to local directories only. A repo name that doesn't pass
    `_valid_repo_name` (e.g. path-traversal characters) is dropped."""
    cache_file = _github_cache_path()
    cached = _read_github_repo_cache(cache_file)
    if cached is not None:
        return cached

    owner = _resolve_github_owner(cache_file)
    if not owner:
        return []

    try:
        result = _run_gh(
            ["gh", "repo", "list", owner, "--json", "name,description", "--limit", "200"],
            timeout=_GH_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return []
        raw = json.loads(result.stdout or "[]")
    except Exception:
        return []
    if not isinstance(raw, list):
        return []

    repos = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not _valid_repo_name(name):
            continue
        description = entry.get("description") or "local project directory"
        repos.append((name, description, _repo_path(name)))

    _write_github_cache(cache_file, owner, repos)
    return repos


def _location_options() -> list[tuple[str, str, str]]:
    """Union of the operator's GitHub repos, scanned local project
    directories, LifeOS, the vault, and home — each as
    (name_lower, description, path). Deduped by lowercase name; a local
    scanned path wins over a GitHub-only entry for the same repo name
    (repos not yet cloned are named by GitHub only)."""
    options: dict[str, tuple[str, str, str]] = {}
    for name, description, path in _github_repos():
        options[name.lower()] = (name.lower(), description, path)
    for name_lower, path in _scan_projects():
        options[name_lower] = (name_lower, "local project directory", path)
    if "lifeos" not in options:
        options["lifeos"] = ("lifeos", "local project directory", _LIFEOS_DIR)
    options["vault"] = ("vault", "the Obsidian vault", str(settings.vault_path))
    options["home"] = ("home", "home directory, for tasks not tied to a project", _HOME)
    return list(options.values())


def ensure_cloned(path: str) -> bool:
    """Clone the operator's GitHub repo into `path` if it isn't there yet.

    Refuses — returns False without ever invoking `gh` — unless `path`
    resolves to exactly `code_root / <name>` for a `name` that both passes
    `_valid_repo_name` and is present in the operator's known repo list
    (`_github_repos()`). This is what stops a manipulated cache file, a
    path-traversal name, or an arbitrary `[working_dir::]` card field from
    ever reaching `gh repo clone`. Returns True once the directory exists
    at `path` (already present, or cloned successfully); False on any
    other failure (`gh` missing, no resolvable owner, network/auth error,
    timeout) — never raises.
    """
    if os.path.isdir(path):
        return True
    code_root = _code_root()
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        return False
    if resolved.parent != code_root:
        return False
    name = resolved.name
    if not _valid_repo_name(name):
        return False
    known_names = {repo_name for repo_name, _desc, _path in _github_repos()}
    if name not in known_names:
        return False
    owner = _resolve_github_owner(_github_cache_path())
    if not owner:
        return False
    try:
        result = _run_gh(
            ["gh", "repo", "clone", f"{owner}/{name}", str(resolved)],
            timeout=_GH_CLONE_TIMEOUT_SECONDS,
        )
    except Exception:
        return False
    return result.returncode == 0 and os.path.isdir(path)


def resolve_working_directory(task: str, *, allow_uncloned: bool = True) -> str:
    """Map a task description to the most appropriate working directory.

    Asks the Jev fan-out judgment (`jev_task_routing.judge_task`) first:
    a `location` answer with confidence >= 0.6 wins outright, resolved
    against `_location_options()` (which may name a GitHub repo not yet
    cloned on this host). Below that confidence, or with no Jev judgment
    at all, falls back to the keyword cascade below unchanged.

    `allow_uncloned=False` rejects a Jev-chosen directory that doesn't
    exist yet on this host (named only via `_github_repos()`, never
    locally scanned or cloned) and falls through to the keyword cascade
    instead — for a spawn this process can't clone into (a remote
    `LIFEOS_AGENT_HOSTS` host), a path with no evidence it exists anywhere
    the CLI will actually run is worse than the keyword guess. Defaults to
    True (today's behavior) for every other caller.
    """
    from api.services.jev_task_routing import judge_task

    judgment = judge_task(task)
    if judgment is not None and judgment.location is not None:
        if judgment.location.confidence >= 0.6 and judgment.location.choice:
            chosen = {name: path for name, _desc, path in _location_options()}.get(
                judgment.location.choice.strip().lower()
            )
            if chosen and (allow_uncloned or os.path.isdir(chosen)):
                return chosen

    task_lower = task.lower()

    # 1. Vault/notes keywords
    for phrase in _VAULT_PHRASES:
        if phrase in task_lower:
            return str(settings.vault_path)
    for word in _VAULT_WORDS:
        if re.search(rf"\b{word}\b", task_lower):
            return str(settings.vault_path)

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
