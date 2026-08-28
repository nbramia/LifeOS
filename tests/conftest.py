"""
Pytest configuration and shared fixtures for LifeOS tests.

Test Categories:
- unit: Fast tests with no external dependencies (< 100ms each)
- slow: Tests requiring ChromaDB, sentence-transformers, or file watchers
- integration: Tests requiring running server or external APIs
- browser: Playwright browser tests
- requires_server: Tests requiring API server to be running

Run categories:
- pytest -m unit              # Fast unit tests only (~60s)
- pytest -m "not slow"        # Skip slow tests
- pytest -m "not integration" # Skip integration tests
- pytest -m browser           # Browser tests only
- pytest                      # All tests
- pytest -n auto              # Parallel execution (requires pytest-xdist)
"""
import gc
import os

import pytest

# git exports GIT_DIR (absolute when the invoker is a linked worktree) to hook
# subprocesses — and the pre-push hook runs this suite. Tests that spawn `git`
# in tmp fixture repos would inherit it and silently operate on the REAL repo:
# committing fixture files onto real branches, flipping core.bare, rewriting
# user identity. Scrub before any test runs so fixture git calls always
# resolve via their own cwd.
for _var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
    os.environ.pop(_var, None)


# ---------------------------------------------------------------------------
# Force CPU-only embeddings under pytest (#521).
#
# This host's iGPU has only 8 SDMA queues. `pytest -n auto` (see
# scripts/test.sh) spawns one worker process per core (16 here), and any
# worker that touches EmbeddingService would independently try to load the
# GPU model — several processes grabbing GPU compute queues at once is
# exactly the concurrency pattern that exhausted the queues and preceded the
# 2026-07-10 host freeze. Tests don't need GPU throughput, so hide the GPU
# unconditionally rather than relying on every future test to remember to.
#
# This MUST be a pytest_configure hook, not an autouse fixture. Fixtures run
# per-test, after collection — by which point pytest has already imported
# every test module (and conftest fixture module), and any of those imports
# could have already pulled in torch/ROCm, which reads these env vars once
# at process start and caches the visible-device list. pytest_configure runs
# immediately after conftest.py itself is imported but *before* pytest
# collects/imports any test module, so it's the latest point that's still
# early enough to change what torch sees.
#
# `scripts/test.sh` also exports these (defense in depth) so anything that
# runs outside pytest itself (e.g. a helper script test.sh shells out to)
# still gets a CPU-only embedding model.
def _force_cpu_embeddings_for_tests() -> None:
    for _var in ("HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES"):
        os.environ.setdefault(_var, "")


def pytest_configure(config):
    """Register custom markers."""
    _force_cpu_embeddings_for_tests()
    config.addinivalue_line("markers", "unit: Fast unit tests")
    config.addinivalue_line("markers", "slow: Slow tests (ChromaDB, embeddings)")
    config.addinivalue_line("markers", "integration: Integration tests (server required)")
    config.addinivalue_line("markers", "browser: Browser tests using Playwright")
    config.addinivalue_line(
        "markers",
        "requires_server: Requires API server running. On a browser test this is "
        "what excludes it from the server-free set the pre-push hook runs.",
    )
    config.addinivalue_line("markers", "requires_db: Requires direct database access (may conflict with running server)")
    config.addinivalue_line(
        "markers",
        "allow_anthropic_api: Test is explicitly allowed to hit api.anthropic.com "
        "(used for the canary test that verifies the ban itself works).",
    )
    _install_anthropic_request_guard()


# ---------------------------------------------------------------------------
# Unmarked-test guard (#682)
#
# The pre-push hook and scripts/test.sh both select tests by marker
# expression (`-m "unit and not slow"` / the negative filter). A test file
# that carries none of the recognized category markers is silently deselected
# from BOTH — it still collects and "exists", so nothing in CI or on push
# reports it as missing. That's exactly how #646 and #677's regression guards
# in tests/test_apple_pipeline.py went unmarked and unrun on push for months.
#
# tryfirst=True so this sees every collected item before pytest's own -m
# deselection hook removes anything — a brand-new unmarked file must fail
# collection even when the current invocation is filtering to `-m unit`.
# ---------------------------------------------------------------------------

_RECOGNIZED_TEST_MARKERS = ("unit", "browser", "integration", "slow", "requires_server")

# The actual hook implementation lives below, merged into the pre-existing
# pytest_collection_modifyitems (search "Parallel execution configuration") —
# a module can only bind one function under that name, so this can't be a
# second top-level def.


# ---------------------------------------------------------------------------
# Anthropic API call guard (#138)
#
# Bleeds happen when a test forgets to mock the LLM client and silently makes
# a real billed call. Guard installs a process-wide httpx transport hook that
# raises if any test attempts a request to api.anthropic.com (or the platform
# console). Tests can opt in to a real call with @pytest.mark.allow_anthropic_api
# — used for the deliberate canary that confirms the guard fires.
# ---------------------------------------------------------------------------

_ANTHROPIC_HOSTS = ("api.anthropic.com", "platform.claude.com")
_anthropic_guard_active = False


def _install_anthropic_request_guard() -> None:
    """Patch httpx so any request to Anthropic's API hosts raises during
    pytest collection. The patch is installed once and stays active for the
    full pytest run. Tests carrying `@pytest.mark.allow_anthropic_api`
    bypass the check.
    """
    global _anthropic_guard_active
    if _anthropic_guard_active:
        return
    import httpx

    original_send = httpx.Client.send
    original_async_send = httpx.AsyncClient.send

    def _is_allowed() -> bool:
        # Read the current test's request fixture indirectly via the pytest
        # node-tracking attribute set by `pytest_runtest_setup`.
        node = getattr(_install_anthropic_request_guard, "_current_node", None)
        if node is None:
            return False
        return node.get_closest_marker("allow_anthropic_api") is not None

    def _guard(request):
        host = (request.url.host or "").lower()
        for blocked in _ANTHROPIC_HOSTS:
            if host == blocked or host.endswith("." + blocked):
                if _is_allowed():
                    return
                raise RuntimeError(
                    f"Test attempted a real Anthropic API call to {request.url}. "
                    "Mock the LLM client (httpx.MockTransport, AsyncMock, etc.) "
                    "or mark the test @pytest.mark.allow_anthropic_api if a "
                    "real call is intended."
                )

    def patched_send(self, request, *args, **kwargs):
        _guard(request)
        return original_send(self, request, *args, **kwargs)

    async def patched_async_send(self, request, *args, **kwargs):
        _guard(request)
        return await original_async_send(self, request, *args, **kwargs)

    httpx.Client.send = patched_send
    httpx.AsyncClient.send = patched_async_send
    _anthropic_guard_active = True


def pytest_runtest_setup(item):
    """Hand the active test item to the guard so it can check markers."""
    _install_anthropic_request_guard._current_node = item


@pytest.fixture(scope="session")
def server_available():
    """Check if API server is available for tests."""
    try:
        import httpx
        response = httpx.get("http://localhost:8000/health", timeout=2.0)
        return response.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def db_available():
    """
    Check if the interactions database is available for direct access.

    When the server is running, it may hold a lock on the SQLite database,
    preventing tests from accessing it directly. This fixture detects that
    situation and allows tests to skip gracefully.
    """
    import sqlite3
    try:
        from api.services.interaction_store import get_interaction_db_path
        db_path = get_interaction_db_path()
        conn = sqlite3.connect(db_path, timeout=1.0)
        # Try to execute a simple query to check for lock
        conn.execute("SELECT 1 FROM interactions LIMIT 1")
        conn.close()
        return True
    except sqlite3.OperationalError:
        return False
    except Exception:
        return False


@pytest.fixture(autouse=False)
def require_db(db_available):
    """Skip test if database is not available (e.g., locked by running server)."""
    if not db_available:
        pytest.skip("Database is locked (server may be running). Stop server to run these tests.")


@pytest.fixture(scope="session")
def test_vault_path(tmp_path_factory):
    """
    Create a temporary vault directory for tests.

    Session-scoped to avoid recreating for every test.
    """
    vault = tmp_path_factory.mktemp("test_vault")
    # Create standard folders
    (vault / "Granola").mkdir()
    (vault / "Work").mkdir()
    (vault / "Personal").mkdir()
    return vault


@pytest.fixture(scope="session")
def test_data_path(tmp_path_factory):
    """
    Create a temporary data directory for ChromaDB and SQLite.

    Session-scoped to share across tests that need persistence.
    """
    return tmp_path_factory.mktemp("test_data")


@pytest.fixture(scope="function")
def mock_settings(test_vault_path, test_data_path, monkeypatch):
    """
    Mock settings for testing.

    Uses temporary paths to avoid affecting real data.
    """
    from config.settings import Settings

    mock = Settings(
        vault_path=test_vault_path,
        chroma_path=test_data_path / "chromadb",
        local_llm_url="http://localhost:8080",
    )

    # Patch the global settings
    monkeypatch.setattr("config.settings.settings", mock)
    return mock


@pytest.fixture(autouse=True)
def _isolate_vault_indexer_stores(tmp_path, monkeypatch):
    """Stop tests from wiping the production vault index.

    ``IndexerService.index_all()`` treats every path in its state file that is
    absent from the current vault as "deleted" and purges it from the Chroma +
    BM25 stores. Tests (test_people, test_integration, test_indexer) build a
    tiny tmp vault and call ``index_all()``; with the real stores attached that
    purge deletes every production note from the ``lifeos_vault`` collection and
    ``data/bm25_index.db``, then rewrites ``data/vault_index_state.json`` with
    the tmp paths. This silently destroyed the live vault index on every test
    run. Redirect the indexer's three stores to throwaway, per-test locations so
    no test can touch production data. The indexer's own ``vector_store`` /
    ``bm25_index`` are reused for the post-index search assertions, so the tests
    stay self-consistent and green.
    """
    import re

    import api.services.indexer as indexer_mod
    from api.services.bm25_index import BM25Index
    from api.services.vectorstore import VectorStore

    monkeypatch.setattr(
        indexer_mod.IndexerService,
        "INDEX_STATE_FILE",
        str(tmp_path / "vault_index_state.json"),
    )

    bm25_db = str(tmp_path / "bm25_index.db")
    monkeypatch.setattr(
        indexer_mod, "BM25Index", lambda *a, **k: BM25Index(db_path=bm25_db)
    )

    collection = "test_vault_" + re.sub(r"[^A-Za-z0-9]", "_", tmp_path.name)[:400]
    created: list = []

    def _isolated_vector_store(*args, **kwargs):
        kwargs["collection_name"] = collection
        store = VectorStore(*args, **kwargs)
        created.append(store)
        return store

    monkeypatch.setattr(indexer_mod, "VectorStore", _isolated_vector_store)

    yield

    # Drop the throwaway collection from the ChromaDB server (best effort).
    if created:
        try:
            created[0]._client.delete_collection(collection)
        except Exception:
            pass


# Collection names `VectorStore` actually writes to on a real install:
# ``lifeos_vault`` (the class default, and the one ``get_vector_store()``'s
# process-wide singleton resolves to), ``lifeos_people``
# (``api/services/person_indexer.py::PERSON_COLLECTION``), ``lifeos_slack``
# (``api/services/slack_indexer.py::SLACK_COLLECTION``), and
# ``lifeos_calendar`` (``api/services/calendar_indexer.py::CALENDAR_COLLECTION``,
# declared there even though that indexer currently writes through the
# vault-default singleton instead — see #828 investigation). Kept here rather
# than imported from those modules to avoid a reverse dependency (they import
# ``VectorStore`` from ``api.services.vectorstore``, not the other way round).
_LIVE_VECTORSTORE_COLLECTIONS = frozenset(
    {"lifeos_vault", "lifeos_people", "lifeos_slack", "lifeos_calendar"}
)


@pytest.fixture(autouse=True)
def _guard_live_vectorstore_collections(request, monkeypatch):
    """Fail loudly if a test constructs a ``VectorStore`` against a real
    production collection on the live ChromaDB server (#828).

    ``VectorStore`` always opens a real HTTP connection
    (``chromadb.HttpClient`` to ``settings.chroma_url``, which defaults to
    the maintainer's real server) — unlike the SQLite-backed stores
    elsewhere in this file, there is no local persist-dir to redirect to.
    ``_isolate_vault_indexer_stores`` above isolates the vault-indexing path
    by renaming the collection before construction; this fixture is the
    backstop for every *other* call site. About thirty rows with
    ``/tmp/tmpXXXXXXXX/vault/...`` file_paths and real vault ``note_type``
    values turned up in the live ``lifeos_vault`` collection (#828) — almost
    certainly debris from a test run that predates (or bypassed) that
    per-collection isolation.

    Rather than patch each of the many ``VectorStore()`` / ``get_vector_store()``
    call sites individually (``api/routes/{admin,chat,search}.py``,
    ``api/services/{hybrid_search,person_indexer,slack_indexer,calendar_indexer}.py``),
    this patches the class's ``__init__`` in place: every one of those call
    sites imported the same class object (``from api.services.vectorstore
    import VectorStore``, or via ``get_vector_store()`` which resolves the
    bare name in ``vectorstore.py``'s own globals either way), so mutating
    its ``__init__`` catches all of them regardless of which module's
    namespace holds the reference — unlike #652's ``SessionStore`` fix,
    which had to swap the *class name* because that bug was about a default
    argument bound once at class-definition time; here we're validating an
    incoming argument, not changing a default, so patching the method
    in place is enough and doesn't disturb ``_isolate_vault_indexer_stores``'s
    own wrapper (it still resolves to the same, now-guarded, class).

    Tests that legitimately need the real store opt in with
    ``@pytest.mark.uses_live_vectorstore`` (e.g. ``test_admin.py``'s
    ``/api/admin/status`` checks, ``test_search_api.py``'s ``/api/search``
    integration tests — both read-only, both require ``TestClient(app)``
    wired to the real service to mean anything).
    """
    if request.node.get_closest_marker("uses_live_vectorstore"):
        return

    import api.services.vectorstore as vectorstore_mod

    _real_init = vectorstore_mod.VectorStore.__init__

    def _guarded_init(self, collection_name="lifeos_vault", server_url=None):
        if collection_name in _LIVE_VECTORSTORE_COLLECTIONS:
            pytest.fail(
                f"{request.node.nodeid} constructed "
                f"VectorStore(collection_name={collection_name!r}), which "
                "points at a live production ChromaDB collection. Isolate "
                "the test with a throwaway collection name (see "
                "_isolate_vault_indexer_stores above), or mark it "
                "@pytest.mark.uses_live_vectorstore if it must legitimately "
                "talk to the live store (#828)."
            )
        _real_init(self, collection_name=collection_name, server_url=server_url)

    monkeypatch.setattr(vectorstore_mod.VectorStore, "__init__", _guarded_init)


@pytest.fixture(autouse=True)
def _isolate_telegram_state_file(tmp_path, monkeypatch):
    """Keep tests off the production Telegram offset file (#357).

    ``TelegramBotListener._STATE_FILE`` defaults to the process-global relative
    path ``data/telegram_state.json``. Any listener built without patching it —
    notably the ones ``api.main``'s lifespan starts for a real ``TestClient``
    app — reads and (via ``_save_last_update_id``) writes that shared file, which
    is the suspected source of the order-dependent flake in
    ``test_primary_uses_legacy_state_file``. Redirect the legacy/primary state
    file to a per-test tmp path so no test touches or leaks the shared default;
    the telegram tests that patch ``_STATE_FILE`` themselves simply nest over
    this. Mirrors ``_isolate_vault_indexer_stores`` above.
    """
    from api.services.telegram import TelegramBotListener

    monkeypatch.setattr(
        TelegramBotListener, "_STATE_FILE", tmp_path / "telegram_state.json"
    )


@pytest.fixture(autouse=True)
def _isolate_conversation_store_db(tmp_path, monkeypatch):
    """Stop tests from opening (and migrating) the production conversations DB.

    ``ConversationStore()`` with no ``db_path`` resolves its path from
    ``settings.chroma_path`` (``get_conversation_db_path()``) — i.e. the real
    ``data/conversations.db`` on whatever machine runs the suite. Any helper or
    fixture that builds a default ``ConversationStore()`` (e.g. the agent-worker
    dispatch helpers that construct ``Worker()`` without an explicit store) would
    otherwise open and run migrations against production data. Redirect the
    default path to a per-test tmp file so no test can ever touch the live DB,
    mirroring ``_isolate_vault_indexer_stores`` above. Tests that inject their own
    ``db_path`` are unaffected (the redirect only changes the default).
    """
    import api.services.conversation_store as conv_store_mod

    monkeypatch.setattr(
        conv_store_mod,
        "get_conversation_db_path",
        lambda: str(tmp_path / "conversations.db"),
    )


@pytest.fixture(autouse=True)
def _stub_conversation_titler(monkeypatch):
    """Stop an ordinary turn-completion test from firing a real background
    call to the local LLM.

    ``conversation_titler.schedule_retitle()`` is wired into the `finally` of
    every native chat turn (``api/routes/chat.py``) and the Hermes/voice
    persistence tees (``api/routes/hermes_proxy.py``, ``api/routes/voice.py``)
    — unlike ``query_router``/``agent_viz_summary`` (mocked per-test, called
    from a handful of call sites), this fires from every turn-completing test
    in the whole suite whenever a conversation happens to reach exactly 2
    user messages. There's no Anthropic-style host guard for the local
    routing LLM it calls (``generate_text`` → the local llama-server), so an
    unmocked test would either hang/slow down waiting on a real model or
    leave a dangling ``asyncio.create_task`` if none is running. Each of the
    three call sites imported the function by name (``from
    api.services.conversation_titler import schedule_retitle``), so it's
    patched at each of those three module namespaces, not just the source
    module. Tests that actually exercise titling behavior — see
    tests/test_conversation_titler.py — call ``conversation_titler``'s
    functions directly instead of relying on this default.
    """
    noop = lambda conversation_id: None  # noqa: E731
    import api.routes.chat as chat_mod
    import api.routes.hermes_proxy as hermes_mod
    import api.routes.voice as voice_mod

    monkeypatch.setattr(chat_mod, "schedule_retitle", noop)
    monkeypatch.setattr(hermes_mod, "schedule_retitle", noop)
    monkeypatch.setattr(voice_mod, "schedule_retitle", noop)


@pytest.fixture(autouse=True)
def _isolate_gmail_draft_ledger(tmp_path, monkeypatch):
    """Stop tests from opening (and writing to) the production Gmail draft
    send-gate ledger (#588).

    ``GmailDraftLedger`` is a process-wide singleton keyed off
    ``settings.chroma_path`` — the real ``data/gmail_draft_ledger.db`` on
    whatever machine runs the suite. It's read and written from three call
    sites now: the `/api/gmail/drafts` and `/api/gmail/send` routes, and the
    in-process `create_email_draft`/`send_email_draft` agent tools. Any test
    exercising any of those without patching it would touch production data.
    Redirect the shared singleton itself (not just one call site's imported
    name) to a per-test tmp instance, mirroring
    ``_isolate_conversation_store_db`` above. Tests that need to seed or
    inspect specific ledger entries (e.g. ``tests/test_gmail_draft_send_gate.py``)
    still patch this same singleton with their own instance, which simply
    overrides this default for that test.
    """
    import api.services.gmail_draft_ledger as ledger_mod

    monkeypatch.setattr(
        ledger_mod,
        "_draft_ledger",
        ledger_mod.GmailDraftLedger(str(tmp_path / "gmail_draft_ledger.db")),
    )


@pytest.fixture(autouse=True)
def _isolate_usage_store_db(tmp_path, monkeypatch):
    """Stop tests from opening (and writing to) the production usage-
    tracking DB (#610).

    ``UsageStore`` is a process-wide singleton (``get_usage_store()``) keyed
    off ``settings.chroma_path`` — the real ``data/usage.db`` on whatever
    machine runs the suite. ``build_turn_context()``
    (``api/services/agent_system_prompt.py``) now calls it on every call to
    look up session-to-date cost, so any test exercising
    ``GET /api/chat/turn-context`` or the Hermes envelope without patching
    it would open (and, via ``record_usage()``, write to) production data.
    The native orchestrator's own system prompt (``build_system_prompt()``)
    does not call ``build_turn_context()`` and is unaffected. Redirect the
    shared singleton itself to a per-test tmp instance, mirroring
    ``_isolate_gmail_draft_ledger`` above — every caller that imports
    ``get_usage_store`` (``hermes_proxy.py``, ``agent_system_prompt.py``,
    ``chat.py``, ...) reads the same patched global, so a test can seed rows
    through any one of them and see them from any other. Tests that need
    their own isolated instance (e.g. tests/test_hermes_proxy.py,
    tests/test_usage_store.py) still patch a specific call site's imported
    name with their own store, which simply overrides this default for that
    test.
    """
    import api.services.usage_store as usage_store_mod

    monkeypatch.setattr(
        usage_store_mod,
        "_usage_store",
        usage_store_mod.UsageStore(str(tmp_path / "usage.db")),
    )


@pytest.fixture(autouse=True)
def _isolate_hermes_persona_thread_store_db(tmp_path, monkeypatch):
    """Stop tests from opening (and writing to) the production Hermes-
    Telegram reply-thread persona mapping (#644 follow-up).

    ``HermesPersonaThreadStore`` is a process-wide singleton
    (``get_persona_thread_store()``) keyed off ``settings.chroma_path`` —
    the real ``data/hermes_persona_threads.db`` on whatever machine runs the
    suite. Redirect the shared singleton itself to a per-test tmp instance,
    mirroring ``_isolate_usage_store_db`` above.
    """
    import api.services.hermes_persona_thread_store as thread_store_mod

    monkeypatch.setattr(
        thread_store_mod,
        "_persona_thread_store",
        thread_store_mod.HermesPersonaThreadStore(str(tmp_path / "hermes_persona_threads.db")),
    )


@pytest.fixture(autouse=True)
def _isolate_session_store_db(tmp_path, monkeypatch):
    """Stop tests from opening (and writing to) the production agent-session
    store (#652).

    Unlike ``ConversationStore`` (which resolves its default path by calling
    ``get_conversation_db_path()`` fresh on every construction, so patching
    that resolver is enough), ``SessionStore``'s default ``db_path`` is a
    plain default argument bound to ``DEFAULT_DB_PATH`` once, when
    ``session_store.py`` is imported — reassigning the module-level
    ``DEFAULT_DB_PATH`` constant afterward has no effect on later bare
    ``SessionStore()`` calls. So instead of patching the path, patch the
    class itself: every call site except a couple of always-overridden ones
    (``Worker.__init__``'s ``session_store or SessionStore()`` fallback,
    never hit because every test constructs ``Worker(session_store=...)``
    explicitly; ``api/routes/agents.py``'s lazy singleton, always
    monkeypatched directly by the handful of tests that touch it) imports
    ``SessionStore`` LOCALLY, inside the function that uses it, specifically
    so tests can replace the class in place —
    ``api/routes/hermes_proxy.py::_resolve_caller_session_id`` documents this
    explicitly. #640 added a caller-session lookup to that function's
    caller, ``_build_envelope()``; ``tests/test_hermes_proxy.py`` sandboxes
    its own tests with a per-test fixture doing exactly this, but every
    *other* test that reaches the Hermes envelope path had no such
    protection and wrote real rows into the operator's live
    ``data/agent_sessions.db``.

    Tests that construct ``SessionStore(db_path=...)`` directly are
    unaffected two ways over: most import the real class at module
    collection time, before this fixture ever runs, so patching the name
    afterward doesn't touch their already-bound reference; and for the local-
    import call sites that DO pass an explicit path (e.g.
    ``tests/test_agent_worker_mcp_exposure.py``'s
    ``SessionStore(db_path=mcp_server.AGENT_SESSIONS_DB)``), the replacement
    below is a subclass that only substitutes the tmp path for the *default*
    argument — an explicit ``db_path`` still passes straight through, same
    as the real class. (A lambda that ignored its arguments and always
    returned one shared instance — the shape ``test_hermes_proxy.py``'s own
    narrower fixture below uses, safely, because the one call site it
    targets never passes an explicit path — would break every explicit-path
    call site an autouse, suite-wide version of it touches.)
    ``tests/test_hermes_proxy.py``'s own ``agent_session_store`` fixture
    composes cleanly for the same reason this file's other isolation
    fixtures do: autouse fixtures set up before a test's explicitly-requested
    ones in the same scope, so its later ``monkeypatch.setattr`` on this same
    class name simply overrides this default for that test.
    """
    import api.services.agent_worker.session_store as session_store_mod

    _RealSessionStore = session_store_mod.SessionStore
    _default_path = tmp_path / "agent_sessions.db"

    class _TestSessionStore(_RealSessionStore):
        def __init__(self, db_path=_default_path):
            super().__init__(db_path)

    monkeypatch.setattr(session_store_mod, "SessionStore", _TestSessionStore)


@pytest.fixture(autouse=True)
def _isolate_transcript_store_dir(tmp_path, monkeypatch):
    """Stop tests from touching the production agent-transcript directory
    (#652 follow-up).

    ``TranscriptStore`` has the identical bound-at-import-time default-
    argument shape as ``SessionStore`` above (``#640`` anchored both the
    same way), so it needs the same class-patching treatment rather than a
    ``DEFAULT_TRANSCRIPTS_DIR`` reassignment. Nothing on the Hermes envelope
    path that motivated #652 touches ``TranscriptStore`` — only
    ``agent_viz_summary_prefetch.py``'s (disabled-by-default-in-tests) busy
    check and ``api/routes/agents.py``'s lazy singleton (already
    monkeypatched directly by the tests that use it) construct a bare one —
    but it's the same bug shape, so it gets the same blanket default rather
    than waiting for a second incident to prove it's needed. Same subclass
    trick as ``_isolate_session_store_db`` above — only the default argument
    is replaced, so an explicit ``transcripts_dir`` still passes through.
    """
    import api.services.agent_worker.transcript_store as transcript_store_mod

    _RealTranscriptStore = transcript_store_mod.TranscriptStore
    _default_dir = tmp_path / "agent_transcripts"

    class _TestTranscriptStore(_RealTranscriptStore):
        def __init__(self, transcripts_dir=_default_dir):
            super().__init__(transcripts_dir)

    monkeypatch.setattr(transcript_store_mod, "TranscriptStore", _TestTranscriptStore)


@pytest.fixture(autouse=True)
def _reset_turn_registry():
    """Reset the #611 chat-turn registry (`api/services/chat_turns.py`)
    before and after every test.

    It's a process-wide singleton holding live `asyncio.Task`s — a turn left
    registered by one test (e.g. one that starts `ask_stream()` and never
    lets its background task finish) would otherwise leak into the next
    test's registry lookups, and a still-running task from a torn-down test
    could touch a store another test has already swapped out. Mirrors
    ``_isolate_conversation_store_db`` above for the same singleton-leakage
    reason.
    """
    from api.services import chat_turns

    chat_turns.reset_turn_registry()
    yield
    chat_turns.reset_turn_registry()


@pytest.fixture(scope="session")
def embedding_service():
    """
    Session-scoped embedding service to avoid repeated model loading.

    Loading sentence-transformers is slow (~2s), so we share one instance.
    """
    try:
        from api.services.embeddings import EmbeddingService
        return EmbeddingService()
    except Exception:
        pytest.skip("Embedding service not available")


# Parallel execution configuration
#
# Also the unmarked-test guard (#682): the pre-push hook and scripts/test.sh
# both select tests by marker expression (`-m "unit and not slow"` / the
# negative filter). A test file that carries none of the recognized category
# markers is silently deselected from BOTH — it still collects and "exists",
# so nothing in CI or on push reports it as missing. That's exactly how #646
# and #677's regression guards in tests/test_apple_pipeline.py went unmarked
# and unrun on push for months.
#
# tryfirst=True so the guard below sees every collected item before pytest's
# own -m deselection hook removes anything — a brand-new unmarked file must
# fail collection even when the current invocation is filtering to `-m unit`.
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """
    Auto-mark tests based on location/name for better organization.

    Tests in test_*_api.py get the 'unit' marker by default.
    Tests with 'browser' in name get the 'browser' marker.
    """
    for item in items:
        # Browser tests
        if "browser" in item.name or "playwright" in str(item.fspath):
            item.add_marker(pytest.mark.browser)

        # #682: the equivalent "integration" name-substring auto-mark used to
        # live here too (`"integration" in item.name or "real_" in item.name`)
        # and is why the pre-push (`-m "unit and not slow"`) and test.sh
        # scopes disagreed by 29 tests — it silently added `integration` to
        # any test merely *named* things like test_real_dns_failed_..., which
        # then carried both `unit` (explicit, correct) and `integration`
        # (false positive from the name match), landing it in the push gate
        # but not test.sh's negative filter. Every test now carries an
        # explicit, correct marker (enforced by the guard below), so this
        # heuristic add is pure liability with no remaining upside. Removed
        # rather than special-cased per file.

    unmarked = [
        item.nodeid
        for item in items
        if not any(item.get_closest_marker(name) for name in _RECOGNIZED_TEST_MARKERS)
    ]
    if not unmarked:
        return

    preview = "\n".join(f"  {nodeid}" for nodeid in unmarked[:20])
    if len(unmarked) > 20:
        preview += f"\n  ...and {len(unmarked) - 20} more"

    raise pytest.UsageError(
        f"{len(unmarked)} test(s) carry none of the recognized category markers "
        f"({', '.join(_RECOGNIZED_TEST_MARKERS)}) and would be silently excluded "
        "from both the pre-push gate (`-m \"unit and not slow\"`) and "
        "`scripts/test.sh`'s default scope. Add exactly one, either as a "
        "decorator or a module-level `pytestmark = pytest.mark.<name>` if it "
        "applies to the whole file:\n"
        "  @pytest.mark.unit            - fast, fully isolated (no real data or live services)\n"
        "  @pytest.mark.integration     - needs real production data or a live external service\n"
        "  @pytest.mark.requires_server - needs the LifeOS API server running on localhost:8000\n"
        "  @pytest.mark.slow            - loads real ML models / ChromaDB / other heavy processing\n"
        "  @pytest.mark.browser         - Playwright browser test\n\n"
        f"Unmarked test(s):\n{preview}"
    )


@pytest.fixture
def production_test_data():
    """
    Load production test data if available, else return None.

    This fixture allows tests to optionally use real production data
    (gitignored) while still passing with generic data in open-source.
    """
    try:
        from tests.fixtures.production_test_data import (
            WORK_DOMAIN,
            TEST_WORK_CONTACT,
            COLLEAGUE_NAMES,
            TEST_PERSONAL_CONTACT,
            TEST_FAMILY_CONTACT,
        )
        return {
            "work_domain": WORK_DOMAIN,
            "test_work_contact": TEST_WORK_CONTACT,
            "colleagues": COLLEAGUE_NAMES,
            "test_personal_contact": TEST_PERSONAL_CONTACT,
            "test_family_contact": TEST_FAMILY_CONTACT,
        }
    except ImportError:
        return None


def pytest_runtest_teardown(item, nextitem):
    """
    Force garbage collection after each test to reduce memory pressure.

    With 1600+ tests, accumulated objects (DB connections, mock objects,
    ChromaDB clients) can push memory to 100%. This hook cleans up after
    each test to keep memory usage bounded.
    """
    _install_anthropic_request_guard._current_node = None
    gc.collect()


@pytest.fixture(autouse=True)
def reset_singletons_after_test():
    """
    Reset lightweight singletons after each test to prevent pollution.

    Singletons that persist across tests can cause:
    - Stale data from previous tests
    - Mock objects leaking between tests
    - Settings changes not taking effect

    This resets fast singletons only (no model reloads).
    """
    yield
    try:
        from tests.reset_singletons import reset_lightweight_singletons
        reset_lightweight_singletons()
    except ImportError:
        pass  # Skip if module not available (shouldn't happen)


@pytest.fixture(scope="session", autouse=True)
def reset_ml_singletons_at_session_end():
    """
    Reset ML singletons at end of test session.

    This ensures the embedding model and other ML resources are
    properly cleaned up when all tests complete.
    """
    yield
    try:
        from tests.reset_singletons import reset_ml_singletons
        reset_ml_singletons()
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _no_local_telegram_bots_override(tmp_path_factory, monkeypatch):
    """Keep a real config/telegram_bots.local.json out of the test run.

    The registry loader prefers the untracked local override over the tracked
    template. Tests monkeypatch ``_TELEGRAM_BOTS_FILE`` (the template), so on a
    machine that actually has a local override the loader would read that
    instead of the fixture and the tests would depend on developer state.
    Point the local path at somewhere that cannot exist.
    """
    missing = tmp_path_factory.mktemp("no-local-bots") / "telegram_bots.local.json"
    monkeypatch.setattr("config.settings._TELEGRAM_BOTS_LOCAL_FILE", missing, raising=False)
