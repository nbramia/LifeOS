"""Query embeddings while another process holds the GPU embedding lock.

A batch job (the nightly sync, a vault reindex) can hold the cross-process GPU
lock for minutes. A search query's embedding waits only briefly for it, then
encodes that one call on a CPU copy of the model, and every async request path
runs the search off the event loop, so the API keeps answering meanwhile.

The lock is a real ``flock`` on a scratch file, held through a second file
descriptor the way another process would hold it. The model is a stub; no real
model is loaded.
"""
import asyncio
import fcntl
import os
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.unit

# Long enough that a blocked event loop is unmistakable, short enough to keep
# the tests quick.
_QUERY_WAIT = 1.5
# The event loop never goes this long without running another coroutine.
_PROMPT = 0.5


def _fake_encode(data, **kwargs):
    if isinstance(data, list):
        return [MagicMock(tolist=lambda: [0.1, 0.2]) for _ in data]
    return MagicMock(tolist=lambda: [0.1, 0.2])


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    from config.settings import settings

    path = tmp_path / "gpu_embed.lock"
    monkeypatch.setattr(settings, "embedding_gpu_lock_enabled", True)
    monkeypatch.setattr(settings, "embedding_gpu_lock_path", str(path))
    monkeypatch.setattr(settings, "embedding_gpu_lock_timeout_seconds", 30.0)
    monkeypatch.setattr(settings, "embedding_query_gpu_lock_timeout_seconds", _QUERY_WAIT)
    return path


@pytest.fixture
def held_lock(lock_path):
    """The GPU lock held by "another process" for the whole test."""
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    yield
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


@pytest.fixture
def models():
    """Stub GPU and CPU models, handed out by device."""
    gpu, cpu = MagicMock(name="gpu"), MagicMock(name="cpu")
    gpu.encode.side_effect = _fake_encode
    cpu.encode.side_effect = _fake_encode
    gpu.max_seq_length = cpu.max_seq_length = 512

    def build(**kwargs):
        return cpu if kwargs.get("device") == "cpu" else gpu

    with patch("sentence_transformers.SentenceTransformer", side_effect=build) as st:
        yield gpu, cpu, st


@pytest.fixture
def service(models, monkeypatch):
    """The process-wide embedding service, with its GPU model already loaded."""
    from api.services import embeddings

    svc = embeddings.EmbeddingService(model_name="test-model")
    _ = svc.model
    monkeypatch.setattr(embeddings, "_embedding_service", svc)
    return svc


class TestQueryLockWait:
    def test_query_gives_up_on_the_lock_within_the_short_wait(self, service, models, held_lock):
        gpu, cpu, _ = models

        start = time.monotonic()
        assert service.embed_text("synthetic query") == [0.1, 0.2]
        elapsed = time.monotonic() - start

        assert _QUERY_WAIT <= elapsed < _QUERY_WAIT + 2
        assert cpu.encode.call_count == 1
        assert gpu.encode.call_count == 0

    def test_query_timeout_leaves_the_process_on_the_gpu(self, service, models, lock_path):
        gpu, cpu, _ = models
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            service.embed_text("synthetic query")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        assert service.using_cpu_fallback is False

        service.embed_text("next synthetic query")
        assert gpu.encode.call_count == 1
        assert cpu.encode.call_count == 1

    def test_a_small_request_path_batch_also_takes_the_short_wait(self, service, models, held_lock):
        gpu, cpu, _ = models

        start = time.monotonic()
        service.embed_texts(["synthetic memory"], query=True)

        assert time.monotonic() - start < _QUERY_WAIT + 2
        assert cpu.encode.call_count == 1
        assert service.using_cpu_fallback is False

    def test_batch_embedding_keeps_the_long_wait(self, service, models, held_lock, monkeypatch):
        """Indexing waits the batch timeout, not the query one, and on timeout
        moves the whole process to CPU."""
        from config.settings import settings

        gpu, cpu, _ = models
        monkeypatch.setattr(settings, "embedding_query_gpu_lock_timeout_seconds", 0.1)
        monkeypatch.setattr(settings, "embedding_gpu_lock_timeout_seconds", 1.5)

        start = time.monotonic()
        service.embed_texts(["synthetic chunk one", "synthetic chunk two"])

        assert time.monotonic() - start >= 1.5
        assert service.using_cpu_fallback is True
        assert gpu.encode.call_count == 0

    def test_default_waits(self):
        from config.settings import Settings

        assert Settings.model_fields["embedding_gpu_lock_timeout_seconds"].default == 300.0
        assert Settings.model_fields["embedding_query_gpu_lock_timeout_seconds"].default == 5.0


class TestCpuCopyLifetime:
    def _contended_query(self, service, lock_path):
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            service.embed_text("synthetic query")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_cpu_copy_is_kept_while_recently_used(self, service, models, lock_path, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(settings, "embedding_query_gpu_lock_timeout_seconds", 0.1)
        self._contended_query(service, lock_path)
        service.embed_text("uncontended synthetic query")

        assert service._cpu_model is models[1]

    def test_idle_cpu_copy_is_released_once_a_query_gets_the_gpu(self, service, models, lock_path, monkeypatch):
        from api.services import embeddings
        from config.settings import settings

        monkeypatch.setattr(settings, "embedding_query_gpu_lock_timeout_seconds", 0.1)
        monkeypatch.setattr(embeddings, "_CPU_COPY_IDLE_RELEASE_SECONDS", 0.0)
        self._contended_query(service, lock_path)
        assert service._cpu_model is models[1]

        service.embed_text("uncontended synthetic query")

        assert service._cpu_model is None
        assert models[0].encode.call_count == 1


class _EmbeddingSearch:
    """Stands in for HybridSearch: embeds the query the way the vector arm does."""

    def search(self, query, **kwargs):
        from api.services.embeddings import get_embedding_service

        get_embedding_service().embed_text(query)
        return []


async def _longest_stall(slow, beat):
    """Run ``slow`` while repeating ``beat``; return (longest beat cycle, slow's result).

    A cycle is measured end to end, so if ``slow`` blocks the event loop the
    cycle that spans the block is as long as the block.
    """
    slow_task = asyncio.create_task(slow)
    longest = 0.0
    last = time.monotonic()
    while not slow_task.done():
        await beat()
        await asyncio.sleep(0.02)
        now = time.monotonic()
        longest = max(longest, now - last)
        last = now
    return longest, await slow_task


class TestEventLoopStaysFree:
    async def test_health_answers_while_a_search_request_waits_for_the_lock(self, service, models, held_lock):
        from api.routes import search as search_route

        app = FastAPI()
        app.include_router(search_route.router)

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        with patch.object(search_route, "get_hybrid_search", return_value=_EmbeddingSearch()):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                async def check_health():
                    assert (await client.get("/health")).status_code == 200

                stall, search_response = await _longest_stall(
                    client.post("/api/search", json={"query": "synthetic query"}),
                    check_health,
                )

        assert search_response.status_code == 200
        assert models[1].encode.call_count == 1  # the query did wait out the lock
        assert stall < _PROMPT

    async def test_chat_turn_memory_lookup_leaves_the_loop_free(self, service, models, held_lock):
        """A chat turn embeds the question for its memory lookup before the
        first model call; other coroutines keep running while it waits."""
        from api.services import agent_loop

        class _MemoryStore:
            def get_relevant_memories(self, query, limit=5):
                from api.services.embeddings import get_embedding_service

                get_embedding_service().embed_text(query)
                return []

        class _Client:
            async def astream(self, messages, **kwargs):
                if False:  # pragma: no cover -- keeps this an async generator
                    yield {}
                raise RuntimeError("synthetic model failure")

        async def turn():
            return [e async for e in agent_loop.run_agent_loop("synthetic question")]

        with patch.object(agent_loop, "_select_client", return_value=_Client()), \
                patch("api.services.memory_store.get_memory_store", return_value=_MemoryStore()):
            stall, events = await _longest_stall(turn(), lambda: asyncio.sleep(0))

        assert any(e["type"] == "text" for e in events)
        assert models[1].encode.call_count == 1  # the lookup did wait out the lock
        assert stall < _PROMPT
