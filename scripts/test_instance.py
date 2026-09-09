#!/usr/bin/env python3
"""
Owned, isolated LifeOS candidate application instances.

A "test instance" is a real ``api.main:app`` FastAPI process — never a
substitute fake app — spawned from an isolated source snapshot
(``candidate_snapshot.py``), on its own port, with a sanitized environment
that excludes the operator's real `.env`, credentials, vault, and databases,
and with background jobs disabled. Ownership is tracked by an explicit,
privately-written manifest (PID + process-start-time marker + candidate
identity + token + an ownership sentinel file), never by "a healthy server
exists on some port" — a healthy server from a different candidate/worktree,
or a reused PID, is rejected rather than accepted.

Usage (also wired as ``./scripts/server.sh test-instance <action>``, which
is the supported entry point — invoke this module directly only for local
debugging):

    ./scripts/server.sh test-instance run    --source . -- <command...>
    ./scripts/server.sh test-instance verify --manifest <path>
    ./scripts/server.sh test-instance stop   --manifest <path>

``run`` is the one supported way to actually exercise a candidate: it
starts an instance, runs the given command with the *same* sanitized
environment and working directory (the candidate snapshot) used for the
server itself — a client test run this way gets no more access to the
operator's real `.env`/paths than the server process does — and always
stops the instance afterward (success, failure, or a signal to the `run`
process itself). There is no detached "start now, stop later from a
separate process" mode: an instance always lives and dies with the process
that started it, which is what lets ownership be a live pipe rather than a
best-effort handoff contract. ``verify``/``stop`` operate on a manifest a
``run`` invocation is still using — useful for a concurrent inspector or a
test's own negative-path checks — never for taking over its lifecycle.

Never touches the shared ``lifeos-api`` service, never calls
``kill_server``/``pkill``/``systemctl``, and only ever signals or deletes
paths/PIDs it can prove it owns (see ``_validate_manifest_ownership``).
"""
from __future__ import annotations

import argparse
import atexit
import http.server
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.candidate_snapshot import build_snapshot, SnapshotResult  # noqa: E402

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a repo dependency
    psutil = None


DEFAULT_VENV_PYTHON = str(Path.home() / ".venvs" / "lifeos" / "bin" / "python")
INSTANCE_ROOT_PREFIX = "lifeos-test-instance-"
DEFAULT_START_TIMEOUT = 120.0  # ML model loading can take a while; see server.sh
_BOOTSTRAP_SCRIPT = Path(__file__).resolve().parent / "_test_instance_bootstrap.py"
_CHROMA_BOOTSTRAP_SCRIPT = Path(__file__).resolve().parent / "_test_chroma_bootstrap.py"
_SUPERVISED_EXEC_BOOTSTRAP = Path(__file__).resolve().parent / "_supervised_exec_bootstrap.py"
_OWNER_SENTINEL_NAME = ".owner"
# create_time() is a stable float read from kernel process-accounting data;
# for the same live PID it returns the identical value on every call. This
# is *not* a grace window for "close enough" — a coarser tolerance would let
# an unrelated process that started within that window pass as a PID-reuse
# match. It only absorbs float round-tripping through JSON.
_START_TIME_TOLERANCE = 1e-6


class InstanceError(RuntimeError):
    pass


class WrongCandidateError(InstanceError):
    """A healthy server exists but doesn't match the manifest's owned identity."""


class InstanceVerificationError(InstanceError):
    pass


class UnownedManifestError(InstanceError):
    """A manifest/instance-dir pair failed ownership validation — refusing to
    signal a PID or delete a path we can't prove we created."""


@dataclass
class InstanceManifest:
    token: str
    candidate_id: str
    source_root: str
    snapshot_root: str
    instance_dir: str
    api_port: int
    base_url: str
    vault_path: str
    chroma_path: str
    data_dir: str
    log_path: str
    llm_stub_url: Optional[str]
    parent_pid: int
    parent_start_time: float
    child_pid: Optional[int]
    child_start_time: Optional[float]
    created_at: float
    chroma_url: Optional[str] = None
    chroma_port: Optional[int] = None
    chroma_pid: Optional[int] = None
    chroma_start_time: Optional[float] = None
    chroma_supervisor_pid: Optional[int] = None
    chroma_supervisor_start_time: Optional[float] = None
    chroma_log_path: Optional[str] = None

    @staticmethod
    def load(path: Path) -> "InstanceManifest":
        payload = json.loads(path.read_text())
        for field in (
            "chroma_url", "chroma_port", "chroma_pid", "chroma_start_time",
            "chroma_supervisor_pid", "chroma_supervisor_start_time", "chroma_log_path",
        ):
            payload.setdefault(field, None)
        return InstanceManifest(**payload)

    def save_atomic(self, path: Path) -> None:
        tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)  # atomic on the same filesystem


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _chroma_executable(python_executable: Optional[str] = None) -> str:
    """Return Chroma from the interpreter selected for this instance."""
    candidates = []
    if python_executable:
        candidates.append(Path(python_executable).with_name("chroma"))
    candidates.append(Path(DEFAULT_VENV_PYTHON).with_name("chroma"))
    for venv_binary in candidates:
        if venv_binary.is_file() and os.access(venv_binary, os.X_OK):
            return str(venv_binary)
    installed = shutil.which("chroma")
    if installed:
        return installed
    raise InstanceError("owned test instance requires an installed chroma executable")


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return False


def _process_start_time(pid: int) -> float:
    if psutil is None:
        raise InstanceError("psutil is required for process identity verification")
    return psutil.Process(pid).create_time()


def _process_matches(pid: int, expected_start_time: float) -> bool:
    if psutil is None:
        raise InstanceError("psutil is required for process identity verification")
    if not psutil.pid_exists(pid):
        return False
    try:
        actual = psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return False
    return abs(actual - expected_start_time) <= _START_TIME_TOLERANCE


def _process_matches_for_stop(pid: int, expected_start_time: float, expected_snapshot_root: str) -> bool:
    """Stronger check than ``_process_matches`` alone before ever signaling a
    PID from a manifest that may not have been created by this same
    in-memory object (e.g. loaded from disk by ``stop_from_manifest``):
    PID + start-time can, in principle, coincide with an unrelated process
    that merely started at the same moment. Also requiring the live
    process's cwd to equal this candidate's own snapshot root ties the
    signal to a process that is actually running *this* candidate.
    """
    if not _process_matches(pid, expected_start_time):
        return False
    if psutil is None:
        return False
    try:
        proc_cwd = psutil.Process(pid).cwd()
    except psutil.Error:
        return False
    return os.path.realpath(proc_cwd) == os.path.realpath(expected_snapshot_root)


def _listener_pid_for_port(port: int) -> Optional[int]:
    """Return the PID of whatever is listening on 127.0.0.1:<port>, if any."""
    if psutil is None:
        raise InstanceError("psutil is required for process identity verification")
    conn_iter = getattr(psutil, "net_connections", None) or psutil.net_connections
    try:
        conns = conn_iter(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        return None
    for c in conns:
        if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
            return c.pid
    return None


def _make_owner_liveness_pipe() -> tuple[int, int]:
    """A portable (Linux + macOS) "die when my owner dies" mechanism: a plain
    POSIX pipe. The orchestrator keeps the write end open for as long as it
    considers itself the instance's owner; the child (see
    ``_test_instance_bootstrap.py``) blocks reading the read end in a
    background thread. When every process holding the write end exits — for
    ANY reason, including a SIGKILL that no atexit/signal handler could ever
    react to — the kernel closes it automatically and the child's read
    returns EOF, which it treats as "my owner is gone" and self-cleans.

    This exists instead of Linux's `prctl(PR_SET_PDEATHSIG)` for two
    reasons: it works on macOS too (no `prctl` there), and it doesn't need a
    `subprocess.Popen(preexec_fn=...)` — running arbitrary Python between
    `fork()` and `exec()` in a parent that already has other threads alive
    (the owned LLM stub server's thread, notably) is exactly the unsafe
    pattern the `subprocess` docs warn against.
    """
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    return read_fd, write_fd


def _validate_manifest_ownership(manifest_path: Path, manifest: InstanceManifest) -> None:
    """Refuse to act on a manifest unless it demonstrably identifies a
    directory *we* created: the manifest must live directly inside the
    instance dir it claims to own, that dir's name must carry our prefix,
    and a private ownership sentinel written at creation time (containing
    the same token) must be present. Without this, a corrupted path, a
    manifest pointed at an arbitrary system PID, or a wrong-directory typo
    could make ``stop`` signal or delete something we don't own.
    """
    manifest_path = manifest_path.resolve()
    instance_dir = Path(manifest.instance_dir).resolve()
    if instance_dir != manifest_path.parent:
        raise UnownedManifestError(
            f"manifest at {manifest_path} claims instance_dir {instance_dir}, "
            f"which is not its own parent directory — refusing to act on it"
        )
    if not instance_dir.name.startswith(INSTANCE_ROOT_PREFIX):
        raise UnownedManifestError(
            f"instance dir {instance_dir} does not carry the expected "
            f"{INSTANCE_ROOT_PREFIX!r} prefix — refusing to act on it"
        )
    sentinel_path = instance_dir / _OWNER_SENTINEL_NAME
    try:
        sentinel_token = sentinel_path.read_text().strip()
    except OSError as e:
        raise UnownedManifestError(
            f"ownership sentinel {sentinel_path} unreadable ({e}) — refusing "
            f"to act on a directory we can't prove we created"
        )
    if sentinel_token != manifest.token:
        raise UnownedManifestError(
            f"ownership sentinel token mismatch for {instance_dir} — refusing "
            f"to act on it (this manifest does not own this directory)"
        )


def _scoped_hf_cache_dir() -> Optional[str]:
    """The model-weights-only subdirectory of the real HF cache — never the
    whole ``HF_HOME``, which can also hold ``token`` (a Hugging Face Hub
    authentication credential). Passing the parent through would expose
    that credential to the isolated instance; this passes only a specific,
    read-intended subdirectory that holds public model snapshots. Not
    enforced read-only at the filesystem level (no OS-level bind-mount
    sandbox is available in this environment — see the network guard's own
    docstring), so this is a scoping choice, not a hard guarantee: honest
    about that rather than claiming isolation this environment can't back.
    """
    # The ordinary embedding cache and the CPU reranker cache are both
    # required by the real search route.  An ambient HF_HUB_CACHE may be a
    # deliberately narrow cache containing only one of them, so prefer it
    # only when it is actually complete.  The standard ``hub`` directory
    # contains public model blobs only; the Hub token lives outside it.
    required_models = (
        "models--mixedbread-ai--mxbai-embed-large-v1",
        "models--cross-encoder--ms-marco-MiniLM-L6-v2",
    )

    def has_required_models(path: Path) -> bool:
        return path.is_dir() and all((path / model).is_dir() for model in required_models)

    configured_cache = os.environ.get("HF_HUB_CACHE")
    if configured_cache and has_required_models(Path(configured_cache)):
        return configured_cache
    hf_home = os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "huggingface")
    hub_cache = Path(hf_home) / "hub"
    if has_required_models(hub_cache):
        return str(hub_cache)
    return str(Path(configured_cache)) if configured_cache else None


def _sanitized_env(manifest: InstanceManifest) -> dict:
    """Build the child's environment from an explicit allowlist, never
    ``os.environ.copy()``. Deliberately excludes any real `.env`-sourced
    variable and API keys. ``HOME`` is a synthetic per-instance directory,
    not the operator's real one, so the candidate can never read real
    dotfiles/SSH keys/credentials cached there. Only a scoped model-weights
    cache subdirectory is passed through (see ``_scoped_hf_cache_dir``), not
    the broader ``HF_HOME``/``XDG_CACHE_HOME`` ambient cache roots, which can
    carry a Hub token or unrelated caches. GPU device-visibility variables
    are forced empty so test instances never contend for the host's shared
    GPU; embeddings fall back to CPU, the already-supported degraded path.
    """
    passthrough_names = ("PATH", "LANG", "LC_ALL", "TZ")
    env = {name: os.environ[name] for name in passthrough_names if name in os.environ}
    scoped_hf_cache = _scoped_hf_cache_dir()
    if scoped_hf_cache:
        env["HF_HUB_CACHE"] = scoped_hf_cache

    env.update({
        "HOME": str(Path(manifest.instance_dir) / "home"),
        "PYTHONPATH": manifest.snapshot_root,
        "PYTHONDONTWRITEBYTECODE": "1",
        "LIFEOS_TEST_INSTANCE": "1",
        "LIFEOS_TEST_TOKEN": manifest.token,
        "LIFEOS_TEST_CANDIDATE_ID": manifest.candidate_id,
        "LIFEOS_TEST_MANIFEST_PATH": str(Path(manifest.instance_dir) / "manifest.json"),
        "LIFEOS_TEST_BASE_URL": manifest.base_url,
        "LIFEOS_PORT": str(manifest.api_port),
        "LIFEOS_HOST": "127.0.0.1",
        "LIFEOS_VAULT_PATH": manifest.vault_path,
        "LIFEOS_CHROMA_PATH": manifest.chroma_path,
        "LIFEOS_CHROMA_URL": manifest.chroma_url or "http://127.0.0.1:1",
        "LIFEOS_CHROMA_PORT": str(manifest.chroma_port or 1),
        "LIFEOS_CHROMA_PID": str(manifest.chroma_pid or ""),
        "ANTHROPIC_API_KEY": "",
        "LIFEOS_LLM_BACKEND": "local",
        "LIFEOS_LOCAL_LLM_URL": manifest.llm_stub_url or "http://127.0.0.1:1",
        # Keep the synthetic timeout trigger bounded for focused chat tests;
        # production settings are never modified by this candidate-only env.
        "LIFEOS_LOCAL_LLM_TIMEOUT": "1",
        # Candidate instances have no network authority.  Failing at model
        # resolution is useful; retrying an intentionally blocked download
        # for tens of seconds is not.  The public CPU caches above must be
        # complete before a real server lane is started.
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        # LIFEOS_SERVER_HOSTNAME is intentionally left unset: the host guard
        # exists to stop a second *production* server, not a test instance.
        "HIP_VISIBLE_DEVICES": "",
        "ROCR_VISIBLE_DEVICES": "",
        "CUDA_VISIBLE_DEVICES": "",
    })
    return env


class _StubLLMHandler(http.server.BaseHTTPRequestHandler):
    """Deterministic OpenAI-compatible chat-completion stub, owned per instance.

    The trigger strings are synthetic and only available through the isolated
    candidate environment.  Ordinary non-stream requests retain the compact
    completion response used by ancillary candidate checks.
    """

    def log_message(self, *_args) -> None:
        pass  # silence — this runs in-process, not worth its own log stream

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        request: dict = {}
        if length:
            try:
                request = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                request = {}
        messages = request.get("messages") or []
        question = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                content = message.get("content", "")
                question = content if isinstance(content, str) else ""
                break

        synthetic_recent_actions = "[synthetic-qa-recent-actions]" in question
        tool_content = next(
            (
                message.get("content", "") for message in reversed(messages)
                if message.get("role") == "tool" and isinstance(message.get("content"), str)
            ),
            "",
        )

        if request.get("stream") and synthetic_recent_actions and not tool_content:
            # First agent round: make a genuine OpenAI-compatible tool call.
            # The application executes it through the normal agent tool
            # registry; no synthetic search payload is injected here.
            frames = [
                {
                    "choices": [{"delta": {"tool_calls": [{
                        "index": 0,
                        "id": "call_synthetic_recent_actions",
                        "type": "function",
                        "function": {
                            "name": "search_vault",
                            "arguments": json.dumps({"query": "action items tasks todo", "top_k": 5}),
                        },
                    }]}, "finish_reason": None}],
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}},
            ]
            self._send_stream_frames(frames)
            return

        if request.get("stream") and synthetic_recent_actions and tool_content:
            # Synthesis derives its facts only from the real tool result.
            source_match = re.search(r"\[\d+\]\s+([^\n]+?)\s+\(score=", tool_content)
            date_match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", tool_content)
            if not source_match or not date_match:
                answer = "Synthetic search_vault returned no usable recent-action result."
            else:
                answer = (
                    f"Recent synthetic action item from {source_match.group(1)} dated "
                    f"{date_match.group(0)}."
                )
            self._send_stream_frames([
                {"choices": [{"delta": {"role": "assistant", "content": answer}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 8, "total_tokens": 11}},
            ])
            return

        # Perf-benchmark triggers: each drives one real tool call through the
        # normal agent tool registry (no synthetic payload injected here —
        # the application executes the call against real, seeded fixture
        # data), then derives its answer only from the real tool result.
        for trigger, tool_name, tool_args, extractor in (
            ("[synthetic-perf-vault]", "search_vault",
             {"query": "synthetic campaign operations checklist", "top_k": 5},
             lambda c: (
                 f"Synthetic vault result from {m.group(1)}."
                 if (m := re.search(r"\[\d+\]\s+([^\n]+?)\s+\(score=", c)) else
                 "Synthetic vault result not found."
             )),
            ("[synthetic-perf-tasks]", "manage_tasks",
             {"action": "list"},
             lambda c: (
                 # Bracket-free: matches tests/synthetic_perf_fixtures.py's
                 # SYNTHETIC_TASK_MARKER, embedded in the seeded task's own
                 # description — a literal "]" is rejected there by
                 # TaskManager._validate_text_fields, unlike this trigger.
                 "Synthetic task found: verify benchmark checklist."
                 if "synthetic-perf-tasks-marker" in c else
                 "Synthetic task not found."
             )),
            ("[synthetic-perf-person]", "person_info",
             {"action": "lookup", "name": "Perf Bench Contact"},
             lambda c: (
                 f"Synthetic person found: {m.group(1)}."
                 if (m := re.search(r"\*\*([^*]+)\*\*", c)) else
                 "Synthetic person not found."
             )),
            ("[synthetic-perf-calendar]", "search_calendar",
             {},
             lambda c: (
                 "Synthetic calendar event found: Quarterly sync."
                 if "[synthetic-perf-calendar]" in c else
                 "Synthetic calendar event not found."
             )),
            ("[synthetic-perf-web]", "search_web",
             {"query": "synthetic perf weather"},
             lambda c: (
                 "Synthetic weather result: Sunny, high of 72F."
                 if "[synthetic-perf-web]" in c else
                 "Synthetic weather result not found."
             )),
        ):
            if trigger not in question:
                continue
            if request.get("stream") and not tool_content:
                frames = [
                    {"choices": [{"delta": {"tool_calls": [{
                        "index": 0,
                        "id": f"call_{tool_name}",
                        "type": "function",
                        "function": {"name": tool_name, "arguments": json.dumps(tool_args)},
                    }]}, "finish_reason": None}]},
                    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}},
                ]
                self._send_stream_frames(frames)
                return
            if request.get("stream") and tool_content:
                answer = extractor(tool_content)
                self._send_stream_frames([
                    {"choices": [{"delta": {"role": "assistant", "content": answer}, "finish_reason": None}]},
                    {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 8, "total_tokens": 11}},
                ])
                return

        if request.get("stream") and "[synthetic-perf-direct]" in question:
            # No tool call: the agent must correctly recognize this query
            # needs no dispatch at all.
            answer = "The capital of France is Paris."
            self._send_stream_frames([
                {"choices": [{"delta": {"role": "assistant", "content": answer}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 6, "total_tokens": 9}},
            ])
            return

        # Synthetic failure/delay controls exercise the actual LocalLLMClient
        # and chat route without credentials, production listeners, or a
        # production-only code path.  Keep the delay below the candidate's
        # one-second test timeout so it cannot make a focused run hang.
        if request.get("stream") and "[synthetic-backend-failure]" in question:
            body = json.dumps({"error": {"message": "synthetic backend failure"}}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                # The client may close immediately after observing the
                # intentional non-2xx status; this is not a provider failure.
                return
            return
        if request.get("stream") and "[synthetic-timeout]" in question:
            time.sleep(2.0)
            return

        body = json.dumps({
            "id": "chatcmpl-synthetic",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "synthetic deterministic test response"},
                "finish_reason": "stop",
            }],
            "model": "lifeos-test-instance-stub",
        }).encode()
        if request.get("stream"):
            frames = [
                {"choices": [{"delta": {"role": "assistant", "content": "synthetic deterministic test response"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}},
            ]
            self._send_stream_frames(frames)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def do_POST(self) -> None:
        self._respond()

    def do_GET(self) -> None:
        self._respond()

    def _send_stream_frames(self, frames: list[dict]) -> None:
        payload = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"
        body = payload.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except BrokenPipeError:
            return


def _start_stub_llm_server() -> tuple[http.server.HTTPServer, threading.Thread, int]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubLLMHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="lifeos-test-instance-llm-stub")
    thread.start()
    return server, thread, port


class TestInstance:
    """Owns exactly one candidate FastAPI process and its private instance dir."""

    __test__ = False  # not a pytest test class, despite the name

    def __init__(
        self,
        source_root: Path,
        *,
        python_executable: Optional[str] = None,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        instances_parent: Optional[Path] = None,
        existing_snapshot: Optional[SnapshotResult] = None,
        process_started: Optional[Callable[[int, float], None]] = None,
    ):
        self.source_root = Path(source_root).resolve()
        self.python_executable = python_executable or (
            DEFAULT_VENV_PYTHON if Path(DEFAULT_VENV_PYTHON).exists() else sys.executable
        )
        self.start_timeout = start_timeout
        self._instances_parent = instances_parent
        self._proc: Optional[subprocess.Popen] = None
        self._chroma_proc: Optional[subprocess.Popen] = None
        self._owner_write_fd: Optional[int] = None
        self._llm_server: Optional[http.server.HTTPServer] = None
        self._llm_thread: Optional[threading.Thread] = None
        self.manifest: Optional[InstanceManifest] = None
        self._manifest_path: Optional[Path] = None
        self._snapshot: Optional[SnapshotResult] = None
        self._existing_snapshot = existing_snapshot
        self._process_started = process_started
        self._cleaned_up = False

    # -- lifecycle -----------------------------------------------------

    def start(self) -> InstanceManifest:
        """Start the candidate, supervised: this object owns the child for as
        long as this process is alive, including surviving a SIGKILL of this
        very process — an owner-liveness pipe the child watches for EOF
        handles that, since no atexit/signal handler could ever react to a
        SIGKILL of its own process. There is no detached/handoff mode: an
        instance always lives and dies with the process that started it (the
        ``run`` CLI action, or this object directly).
        """
        instance_dir = Path(tempfile.mkdtemp(prefix=INSTANCE_ROOT_PREFIX, dir=self._instances_parent))
        os.chmod(instance_dir, 0o700)
        try:
            return self._start_inner(instance_dir)
        except Exception:
            # Cleanup covers every failure point, including ones before a
            # manifest ever existed (e.g. the snapshot itself failing) —
            # never leaks the instance dir or an already-spawned child.
            if self._owner_write_fd is not None:
                try:
                    os.close(self._owner_write_fd)
                except OSError:
                    pass
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=5)
                except Exception:
                    pass
            if self._chroma_proc is not None and self._chroma_proc.poll() is None:
                try:
                    os.killpg(self._chroma_proc.pid, signal.SIGKILL)
                    self._chroma_proc.wait(timeout=5)
                except Exception:
                    pass
            if self._llm_server is not None:
                self._llm_server.shutdown()
            shutil.rmtree(instance_dir, ignore_errors=True)
            raise

    def _start_inner(self, instance_dir: Path) -> InstanceManifest:
        # Ownership sentinel written before anything else touches this
        # directory — stop()/verify() refuse to act without it matching.
        token = secrets.token_hex(16)
        (instance_dir / _OWNER_SENTINEL_NAME).write_text(token)
        os.chmod(instance_dir / _OWNER_SENTINEL_NAME, 0o600)

        snapshot_root = instance_dir / "src"
        vault_path = instance_dir / "vault"
        chroma_path = instance_dir / "chromadb"
        data_dir = instance_dir / "data"
        home_dir = instance_dir / "home"
        log_path = instance_dir / "server.log"
        for d in (vault_path, chroma_path, data_dir, home_dir):
            d.mkdir(mode=0o700)

        if self._existing_snapshot is None:
            self._snapshot = build_snapshot(self.source_root, snapshot_root)
        else:
            if Path(self._existing_snapshot.dest_root).resolve() != self.source_root:
                raise InstanceError("existing snapshot does not match test-instance source")
            self._snapshot = self._existing_snapshot
            snapshot_root = Path(self._snapshot.dest_root)

        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        chroma_port = _free_port()
        chroma_url = f"http://127.0.0.1:{chroma_port}"
        chroma_log_path = instance_dir / "chromadb.log"
        self._llm_server, self._llm_thread, llm_port = _start_stub_llm_server()
        llm_stub_url = f"http://127.0.0.1:{llm_port}"

        manifest = InstanceManifest(
            token=token,
            candidate_id=self._snapshot.candidate_id,
            source_root=str(self.source_root),
            snapshot_root=str(snapshot_root),
            instance_dir=str(instance_dir),
            api_port=port,
            base_url=base_url,
            vault_path=str(vault_path),
            chroma_path=str(chroma_path),
            data_dir=str(data_dir),
            log_path=str(log_path),
            llm_stub_url=llm_stub_url,
            parent_pid=os.getpid(),
            parent_start_time=_process_start_time(os.getpid()),
            child_pid=None,
            child_start_time=None,
            created_at=time.time(),
            chroma_url=chroma_url,
            chroma_port=chroma_port,
            chroma_log_path=str(chroma_log_path),
        )
        manifest_path = instance_dir / "manifest.json"
        manifest.save_atomic(manifest_path)
        self._manifest_path = manifest_path
        self.manifest = manifest

        env = _sanitized_env(manifest)
        owner_read_fd, self._owner_write_fd = _make_owner_liveness_pipe()
        chroma_cmd = [
            self.python_executable, str(_CHROMA_BOOTSTRAP_SCRIPT),
            "--host", "127.0.0.1", "--port", str(chroma_port),
            "--path", str(chroma_path), "--instance-dir", str(instance_dir),
            "--chroma-executable", _chroma_executable(self.python_executable),
            "--owner-liveness-fd", str(owner_read_fd),
        ]
        with open(chroma_log_path, "ab") as chroma_log:
            self._chroma_proc = subprocess.Popen(
                chroma_cmd,
                cwd=str(snapshot_root), env=env,
                stdout=chroma_log, stderr=subprocess.STDOUT,
                start_new_session=True, pass_fds=(owner_read_fd,),
            )
        manifest.chroma_supervisor_pid = self._chroma_proc.pid
        manifest.chroma_supervisor_start_time = _process_start_time(self._chroma_proc.pid)
        manifest.save_atomic(manifest_path)
        self.manifest = manifest
        if self._process_started is not None:
            self._process_started(
                manifest.chroma_supervisor_pid, manifest.chroma_supervisor_start_time,
            )
        self._wait_chroma_healthy()
        # The API and its test command receive the discovered service PID as
        # well as the fixed private URL/port; the Chroma wrapper itself did
        # not need that child identity before it launched.
        env = _sanitized_env(manifest)

        cmd = [self.python_executable, str(_BOOTSTRAP_SCRIPT),
               "--host", "127.0.0.1", "--port", str(port),
               "--instance-dir", str(instance_dir),
               "--owner-liveness-fd", str(owner_read_fd)]
        with open(log_path, "ab") as log_file:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(snapshot_root),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group; never inherits our controlling tty
                pass_fds=(owner_read_fd,),
            )
        os.close(owner_read_fd)  # the child has its own copy now; ours would keep it alive
        manifest.child_pid = self._proc.pid
        manifest.child_start_time = _process_start_time(self._proc.pid)
        manifest.save_atomic(manifest_path)
        self.manifest = manifest
        if self._process_started is not None:
            self._process_started(manifest.child_pid, manifest.child_start_time)

        self._wait_healthy()
        self.verify()
        return manifest

    def _wait_chroma_healthy(self) -> None:
        if self.manifest is None or self._chroma_proc is None:
            raise InstanceError("owned Chroma process was not initialized")
        deadline = time.time() + self.start_timeout
        heartbeat = f"{self.manifest.chroma_url}/api/v2/heartbeat"
        while time.time() < deadline:
            if self._chroma_proc.poll() is not None:
                raise InstanceError(
                    f"owned Chroma supervisor exited early (code {self._chroma_proc.returncode}); "
                    f"see {self.manifest.chroma_log_path}"
                )
            if _http_ok(heartbeat):
                listener_pid = _listener_pid_for_port(self.manifest.chroma_port)
                if listener_pid is not None:
                    self.manifest.chroma_pid = listener_pid
                    self.manifest.chroma_start_time = _process_start_time(listener_pid)
                    self.manifest.save_atomic(self._manifest_path)
                    return
            time.sleep(0.1)
        raise InstanceError(
            f"owned Chroma did not become healthy within {self.start_timeout}s; "
            f"see {self.manifest.chroma_log_path}"
        )

    def _wait_healthy(self) -> None:
        deadline = time.time() + self.start_timeout
        last_err: Optional[BaseException] = None
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise InstanceError(
                    f"candidate process exited early (code {self._proc.returncode}); "
                    f"see {self.manifest.log_path}"
                )
            try:
                with urllib.request.urlopen(f"{self.manifest.base_url}/health", timeout=2) as resp:
                    if 200 <= resp.status < 300:
                        return
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                last_err = e
            time.sleep(0.5)
        raise InstanceError(
            f"candidate did not become healthy within {self.start_timeout}s "
            f"(last error: {last_err}); see {self.manifest.log_path}"
        )

    def verify(self) -> None:
        """Independently confirm the manifest's owned process is what's actually
        listening — a healthy response alone is never sufficient identity proof.
        """
        m = self.manifest
        if m is None:
            raise InstanceVerificationError("no manifest to verify")
        if not _process_matches(m.child_pid, m.child_start_time):
            raise WrongCandidateError(
                f"PID {m.child_pid} is gone or was reused (start-time marker mismatch); "
                f"this manifest no longer identifies a real owned process"
            )
        listener_pid = _listener_pid_for_port(m.api_port)
        if listener_pid is None:
            raise WrongCandidateError(f"nothing is listening on port {m.api_port}")
        if listener_pid != m.child_pid:
            raise WrongCandidateError(
                f"port {m.api_port} is held by PID {listener_pid}, not our owned "
                f"PID {m.child_pid} — refusing to treat a foreign listener as this instance"
            )
        if psutil is not None:
            try:
                proc_cwd = psutil.Process(m.child_pid).cwd()
            except psutil.Error:
                proc_cwd = None
            if proc_cwd is not None and os.path.realpath(proc_cwd) != os.path.realpath(m.snapshot_root):
                raise WrongCandidateError(
                    f"owned process cwd {proc_cwd!r} does not match this candidate's "
                    f"snapshot root {m.snapshot_root!r}"
                )
        try:
            with urllib.request.urlopen(f"{m.base_url}/health", timeout=5) as resp:
                if not (200 <= resp.status < 300):
                    raise WrongCandidateError(f"/health returned status {resp.status}")
        except urllib.error.URLError as e:
            raise WrongCandidateError(f"/health unreachable at {m.base_url}: {e}")
        try:
            with urllib.request.urlopen(f"{m.base_url}/_test_instance/identity", timeout=5) as resp:
                identity = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError, UnicodeDecodeError) as e:
            raise WrongCandidateError("owned candidate identity endpoint is unreachable") from e
        if identity.get("candidate_id") != m.candidate_id or identity.get("token") != m.token:
            raise WrongCandidateError("owned candidate identity endpoint does not match manifest")
        self._verify_chroma()

    def _verify_chroma(self) -> None:
        m = self.manifest
        if (
            m is None or not m.chroma_url or m.chroma_port is None
            or m.chroma_pid is None or m.chroma_start_time is None
            or m.chroma_supervisor_pid is None or m.chroma_supervisor_start_time is None
        ):
            raise WrongCandidateError("owned Chroma identity is missing from manifest")
        if not _process_matches(m.chroma_pid, m.chroma_start_time):
            raise WrongCandidateError("owned Chroma PID is gone or was reused")
        if not _process_matches_for_stop(
            m.chroma_supervisor_pid, m.chroma_supervisor_start_time, m.snapshot_root,
        ):
            raise WrongCandidateError("owned Chroma supervisor no longer matches manifest")
        listener_pid = _listener_pid_for_port(m.chroma_port)
        if listener_pid != m.chroma_pid:
            raise WrongCandidateError("owned Chroma port is not held by its recorded process")
        if not _http_ok(f"{m.chroma_url}/api/v2/heartbeat"):
            raise WrongCandidateError("owned Chroma heartbeat is unreachable")

    def command_environment(self) -> dict:
        """Return the exact private environment bound to this owned instance."""
        if self.manifest is None:
            raise InstanceError("test instance has not started")
        return _sanitized_env(self.manifest)

    def stop(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
        ownership_validated = False
        try:
            if self.manifest and self._manifest_path:
                _validate_manifest_ownership(self._manifest_path, self.manifest)
                ownership_validated = True
                if self.manifest.child_pid and _process_matches_for_stop(
                    self.manifest.child_pid, self.manifest.child_start_time,
                    self.manifest.snapshot_root,
                ):
                    _terminate_pid(self.manifest.child_pid, process=self._proc)
                if (
                    self.manifest.chroma_supervisor_pid
                    and self.manifest.chroma_supervisor_start_time is not None
                    and _process_matches_for_stop(
                    self.manifest.chroma_supervisor_pid,
                    self.manifest.chroma_supervisor_start_time,
                    self.manifest.snapshot_root,
                    )
                ):
                    _terminate_pid(
                        self.manifest.chroma_supervisor_pid, process=self._chroma_proc,
                    )
        finally:
            if self._owner_write_fd is not None:
                try:
                    os.close(self._owner_write_fd)
                except OSError:
                    pass
                self._owner_write_fd = None
            if self._llm_server is not None:
                self._llm_server.shutdown()
                self._llm_server.server_close()
            # Only remove the directory if ownership was actually confirmed
            # — an ownership-validation failure above must never fall
            # through to deleting the (unproven) directory anyway.
            if self.manifest and ownership_validated:
                shutil.rmtree(self.manifest.instance_dir, ignore_errors=True)
            try:
                atexit.unregister(self.stop)
            except Exception:
                pass

    def __enter__(self) -> "TestInstance":
        self.start()
        # Context-manager usage (tests) owns the instance for its own
        # process lifetime, so an atexit safety net is appropriate here even
        # though `__exit__` already covers the normal case — this only ever
        # fires as a backstop against a caller forgetting `__exit__`/`stop`.
        atexit.register(self.stop)
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()


def _terminate_pid(pid: int, timeout: float = 10.0, process: Optional[subprocess.Popen] = None) -> None:
    """SIGTERM then SIGKILL — never a broad pkill/name match, only this exact PID."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process is not None:
            if process.poll() is not None:
                process.wait()
                return
        if psutil is not None and not psutil.pid_exists(pid):
            return
        if psutil is None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_from_manifest(manifest_path: Path) -> None:
    manifest_path = manifest_path.resolve()
    manifest = InstanceManifest.load(manifest_path)
    _validate_manifest_ownership(manifest_path, manifest)
    if manifest.child_pid and _process_matches_for_stop(
        manifest.child_pid, manifest.child_start_time, manifest.snapshot_root,
    ):
        _terminate_pid(manifest.child_pid)
    if (
        manifest.chroma_supervisor_pid
        and manifest.chroma_supervisor_start_time is not None
        and _process_matches_for_stop(
        manifest.chroma_supervisor_pid, manifest.chroma_supervisor_start_time,
        manifest.snapshot_root,
        )
    ):
        _terminate_pid(manifest.chroma_supervisor_pid)
    shutil.rmtree(manifest.instance_dir, ignore_errors=True)


def verify_from_manifest(manifest_path: Path) -> None:
    manifest_path = manifest_path.resolve()
    manifest = InstanceManifest.load(manifest_path)
    _validate_manifest_ownership(manifest_path, manifest)
    inst = TestInstance(Path(manifest.source_root))
    inst.manifest = manifest
    inst.verify()


def run_supervised_command(
    command: list[str], *, cwd: str, env: dict,
    process_started: Optional[Callable[[int, float], None]] = None,
) -> subprocess.CompletedProcess:
    """Run ``command`` in ``cwd`` with ``env``, under the same portable
    owner-liveness supervision contract a candidate instance's own child
    gets — if the calling process is killed, including a SIGKILL, so is
    ``command`` (and anything it spawned, unless that descendant detaches
    into its own session). No server is started and no snapshot is taken
    here: this is the reusable primitive underneath that, for any caller
    that already has its own ``SnapshotResult.dest_root`` and sanitized
    environment and wants the supervision guarantee without duplicating the
    pipe-watching thread.

    The wrapper process is spawned as its own session leader
    (``start_new_session=True``), so its pid *is* the process-group id for
    it, ``command``, and anything ``command`` spawns without detaching.
    That means this function can reach the whole group directly — via
    ``proc.pid`` — even if it has to kill the wrapper itself outright (the
    ``except`` branch below): the wrapper's own ``finally``-based group-kill
    never gets a chance to run under a SIGKILL, so this function performs
    the same group-kill independently rather than only stopping the wrapper.
    """
    owner_read_fd, owner_write_fd = _make_owner_liveness_pipe()
    try:
        proc = subprocess.Popen(
            [sys.executable, str(_SUPERVISED_EXEC_BOOTSTRAP),
             "--owner-liveness-fd", str(owner_read_fd), "--", *command],
            cwd=cwd, env=env, start_new_session=True, pass_fds=(owner_read_fd,),
        )
        os.close(owner_read_fd)
        try:
            if process_started is not None:
                process_started(proc.pid, _process_start_time(proc.pid))
            proc.wait()
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # proc.pid IS the group id
            except OSError:
                pass
            proc.wait()
            raise
        return subprocess.CompletedProcess(command, proc.returncode)
    finally:
        try:
            os.close(owner_write_fd)
        except OSError:
            pass


# -- CLI -----------------------------------------------------------------

def _install_signal_traps(inst: TestInstance) -> None:
    def _handler(signum, _frame):
        inst.stop()
        sys.exit(128 + signum)
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    p_verify = sub.add_parser("verify", help="verify an existing instance's identity")
    p_verify.add_argument("--manifest", required=True, type=Path)

    p_stop = sub.add_parser("stop", help="stop an owned instance and remove its files")
    p_stop.add_argument("--manifest", required=True, type=Path)

    p_run = sub.add_parser(
        "run", help="start, run a command with the instance's env/cwd, then always stop"
    )
    p_run.add_argument("--source", default=".", type=Path)
    p_run.add_argument("--timeout", type=float, default=DEFAULT_START_TIMEOUT)
    p_run.add_argument("command", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)

    if args.action == "verify":
        try:
            verify_from_manifest(args.manifest)
        except InstanceError as e:
            print(f"REJECTED: {e}", file=sys.stderr)
            return 1
        print("OK")
        return 0

    if args.action == "stop":
        stop_from_manifest(args.manifest)
        return 0

    if args.action == "run":
        command = args.command
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            parser.error("run requires a command after --")
        inst = TestInstance(args.source, start_timeout=args.timeout)
        _install_signal_traps(inst)
        manifest = inst.start()
        print(f"candidate_id={manifest.candidate_id}", flush=True)
        print(f"base_url={manifest.base_url}", flush=True)
        print(f"instance_dir={manifest.instance_dir}", flush=True)
        print(f"manifest_path={inst._manifest_path}", flush=True)
        print(f"vault_path={manifest.vault_path}", flush=True)
        print(f"chroma_url={manifest.chroma_url}", flush=True)
        print(f"chroma_port={manifest.chroma_port}", flush=True)
        print(f"chroma_pid={manifest.chroma_pid}", flush=True)
        try:
            # Same sanitized environment and working directory as the
            # server itself — a client test run this way gets no more
            # access to the operator's real .env/paths than the server does.
            env = _sanitized_env(manifest)
            result = subprocess.run(command, cwd=manifest.snapshot_root, env=env)
            return result.returncode
        finally:
            inst.stop()

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
