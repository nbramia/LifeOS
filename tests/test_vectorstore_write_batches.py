"""Writing a note's chunks to Chroma in batches within the server's limit.

The store is a real in-process Chroma client, which enforces the same
per-write record limit as the server. Only the embedding model is replaced.
"""
from __future__ import annotations

import itertools
import json
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

FILE_PATH = "/synthetic/vault/note.md"
_collection_numbers = itertools.count()


class _FakeEmbeddingService:
    def embed_texts(self, texts):
        return [[float(len(text) % 7), 1.0, 0.5, 0.25] for text in texts]


def _bare_store(client, collection):
    from api.services.vectorstore import VectorStore

    store = VectorStore.__new__(VectorStore)
    store._client = client
    store._collection = collection
    store._embedding_service = _FakeEmbeddingService()
    return store


@pytest.fixture
def chroma():
    import chromadb

    client = chromadb.EphemeralClient()
    name = f"synthetic-batches-{next(_collection_numbers)}"
    collection = client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})
    yield client, collection
    client.delete_collection(name)


@pytest.fixture
def store(chroma):
    return _bare_store(*chroma)


def _chunks(count: int, text: str = "synthetic chunk") -> list[dict]:
    return [{"content": f"{text} {i}", "chunk_index": i} for i in range(count)]


def _metadata(**extra) -> dict:
    return {"file_path": FILE_PATH, "file_name": "note.md", "modified_date": "2026-01-02", **extra}


def _stored_ids(collection) -> set[str]:
    return set(collection.get(where={"file_path": FILE_PATH}, include=[])["ids"])


def _ids(count: int) -> set[str]:
    return {f"{FILE_PATH}::{i}" for i in range(count)}


class TestLimit:
    def test_the_client_enforces_a_limit_a_whole_note_can_exceed(self, chroma):
        client, collection = chroma
        limit = client.get_max_batch_size()

        with pytest.raises(Exception, match="batch size"):
            collection.add(ids=[str(i) for i in range(limit + 1)], embeddings=[[0.1, 0.2, 0.3, 0.4]] * (limit + 1))

    def test_add_document_writes_a_note_larger_than_the_limit(self, store, chroma):
        client, collection = chroma
        count = client.get_max_batch_size() + 1

        store.add_document(_chunks(count), _metadata())

        assert _stored_ids(collection) == _ids(count)

    def test_update_document_writes_a_note_larger_than_the_limit(self, store, chroma):
        client, collection = chroma
        count = client.get_max_batch_size() + 1
        store.add_document(_chunks(3, "old"), _metadata())

        store.update_document(_chunks(count, "new"), _metadata())

        assert _stored_ids(collection) == _ids(count)
        assert collection.get(ids=[f"{FILE_PATH}::0"])["documents"] == ["new 0"]

    def test_delete_document_removes_a_note_larger_than_the_limit(self, store, chroma):
        client, collection = chroma
        store.add_document(_chunks(client.get_max_batch_size() + 1), _metadata())

        store.delete_document(FILE_PATH)

        assert _stored_ids(collection) == set()


class TestReplacement:
    def test_new_text_replaces_old_and_chunks_the_note_no_longer_has_are_removed(self, store, chroma):
        _, collection = chroma
        store.add_document(_chunks(8, "old"), _metadata(tags=["alpha"]))

        store.update_document(_chunks(3, "new"), _metadata(tags=["beta"]))

        stored = collection.get(where={"file_path": FILE_PATH}, include=["documents", "metadatas"])
        assert sorted(stored["documents"]) == ["new 0", "new 1", "new 2"]
        assert {json.loads(meta["tags"])[0] for meta in stored["metadatas"]} == {"beta"}
        assert not any("tag:alpha" in meta for meta in stored["metadatas"])

    def test_other_notes_are_untouched(self, store, chroma):
        _, collection = chroma
        other = {"file_path": "/synthetic/vault/other.md", "file_name": "other.md"}
        store.add_document(_chunks(2, "other"), other)
        store.add_document(_chunks(2, "old"), _metadata())

        store.update_document(_chunks(1, "new"), _metadata())

        assert len(collection.get(where={"file_path": other["file_path"]})["ids"]) == 2

    def test_note_shrinking_from_more_than_the_limit_removes_its_stale_chunks(self, store, chroma):
        client, collection = chroma
        store.add_document(_chunks(client.get_max_batch_size() + 1), _metadata())

        store.update_document(_chunks(2, "new"), _metadata())

        assert _stored_ids(collection) == _ids(2)

    def test_update_to_no_chunks_removes_the_note(self, store, chroma):
        _, collection = chroma
        store.add_document(_chunks(2), _metadata())

        store.update_document([], _metadata())

        assert _stored_ids(collection) == set()

    def test_update_of_a_note_not_yet_stored_adds_it(self, store, chroma):
        _, collection = chroma

        store.update_document(_chunks(2), _metadata())

        assert _stored_ids(collection) == _ids(2)


class TestPartialFailure:
    def _fail_second_write(self, collection, monkeypatch):
        real_add = collection.add
        calls = []

        def add(**kwargs):
            calls.append(len(kwargs["ids"]))
            if len(calls) == 2:
                raise RuntimeError("write failed")
            return real_add(**kwargs)

        monkeypatch.setattr(collection, "add", add)
        return calls

    def test_failed_write_leaves_batches_not_reached_as_they_were(self, store, chroma, monkeypatch):
        _, collection = chroma
        store.add_document(_chunks(6, "old"), _metadata())
        store._reported_write_batch = 2
        self._fail_second_write(collection, monkeypatch)

        with pytest.raises(RuntimeError):
            store.update_document(_chunks(4, "new"), _metadata())

        got = collection.get(where={"file_path": FILE_PATH}, include=["documents"])
        stored = dict(zip(got["ids"], got["documents"]))
        assert stored == {
            f"{FILE_PATH}::0": "new 0", f"{FILE_PATH}::1": "new 1",
            f"{FILE_PATH}::4": "old 4", f"{FILE_PATH}::5": "old 5",
        }

    def test_retry_after_a_failed_write_completes_the_replacement(self, store, chroma, monkeypatch):
        _, collection = chroma
        store.add_document(_chunks(6, "old"), _metadata())
        store._reported_write_batch = 2
        self._fail_second_write(collection, monkeypatch)
        with pytest.raises(RuntimeError):
            store.update_document(_chunks(4, "new"), _metadata())
        monkeypatch.undo()

        store.update_document(_chunks(4, "new"), _metadata())

        stored = collection.get(where={"file_path": FILE_PATH}, include=["documents"])
        assert sorted(stored["documents"]) == ["new 0", "new 1", "new 2", "new 3"]
        assert _stored_ids(collection) == _ids(4)

    def test_failed_embedding_leaves_the_note_as_it_was(self, store, chroma, monkeypatch):
        _, collection = chroma
        store.add_document(_chunks(3, "old"), _metadata())
        monkeypatch.setattr(store._embedding_service, "embed_texts", MagicMock(side_effect=RuntimeError("embed")))

        with pytest.raises(RuntimeError):
            store.update_document(_chunks(5, "new"), _metadata())

        assert collection.get(where={"file_path": FILE_PATH}, include=["documents"])["documents"] == [
            "old 0", "old 1", "old 2"
        ]


class TestBatchSize:
    class _Recorder:
        def __init__(self):
            self.writes = []

        def _record(self, kind, kwargs):
            self.writes.append((kind, len(kwargs["ids"])))

        def add(self, **kwargs):
            self._record("add", kwargs)

        def upsert(self, **kwargs):
            self._record("upsert", kwargs)

        def get(self, **kwargs):
            return {"ids": []}

        def delete(self, **kwargs):
            self.writes.append(("delete", len(kwargs["ids"])))

    def test_batches_follow_the_size_the_client_reports(self):
        client = MagicMock()
        client.get_max_batch_size.return_value = 4
        recorder = self._Recorder()

        _bare_store(client, recorder).add_document(_chunks(10), _metadata())

        assert recorder.writes == [("add", 4), ("add", 4), ("add", 2)]

    def test_client_without_the_method_falls_back_to_a_safe_size(self):
        from api.services.vectorstore import FALLBACK_WRITE_BATCH

        recorder = self._Recorder()

        _bare_store(object(), recorder).add_document(_chunks(FALLBACK_WRITE_BATCH + 1), _metadata())

        assert recorder.writes == [("add", FALLBACK_WRITE_BATCH), ("add", 1)]

    def test_client_whose_method_fails_falls_back_to_a_safe_size(self):
        from api.services.vectorstore import FALLBACK_WRITE_BATCH

        client = MagicMock()
        client.get_max_batch_size.side_effect = ConnectionError
        recorder = self._Recorder()

        _bare_store(client, recorder).add_document(_chunks(FALLBACK_WRITE_BATCH + 1), _metadata())

        assert recorder.writes == [("add", FALLBACK_WRITE_BATCH), ("add", 1)]

    def test_client_reporting_no_usable_size_falls_back_to_a_safe_size(self):
        from api.services.vectorstore import FALLBACK_WRITE_BATCH

        client = MagicMock()
        client.get_max_batch_size.return_value = 0
        recorder = self._Recorder()

        _bare_store(client, recorder).add_document(_chunks(FALLBACK_WRITE_BATCH + 1), _metadata())

        assert recorder.writes == [("add", FALLBACK_WRITE_BATCH), ("add", 1)]

    def test_fallback_stays_below_the_known_server_limit(self):
        from api.services.vectorstore import FALLBACK_WRITE_BATCH

        assert 0 < FALLBACK_WRITE_BATCH < 5461

    def test_size_is_asked_for_once(self):
        client = MagicMock()
        client.get_max_batch_size.return_value = 5
        store = _bare_store(client, self._Recorder())

        store.add_document(_chunks(3), _metadata())
        store.add_document(_chunks(3), _metadata())

        assert client.get_max_batch_size.call_count == 1


class TestThroughTheIndexer:
    def test_index_file_stores_a_note_with_more_chunks_than_the_limit(self, tmp_path, chroma, monkeypatch):
        import api.services.indexer as indexer_mod
        from api.services.indexer import IndexerService

        client, collection = chroma
        count = client.get_max_batch_size() + 5
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "huge.md"
        note.write_text("# huge\n\nsynthetic text\n")
        monkeypatch.setattr(indexer_mod, "chunk_document", lambda *a, **k: _chunks(count))
        monkeypatch.setattr(indexer_mod, "add_context_to_chunks", lambda chunks, *a, **k: chunks)
        monkeypatch.setattr("api.services.summarizer.queue_summary", MagicMock())
        indexer = IndexerService(
            vault_path=str(vault), tag_store=MagicMock(get=MagicMock(return_value=None)), interaction_store=MagicMock(),
            source_entity_store=MagicMock(), entity_resolver=MagicMock(),
        )
        indexer.vector_store = _bare_store(client, collection)

        indexer.index_file(str(note), light_pass=True)

        assert len(collection.get(where={"file_path": str(note.resolve())}, include=[])["ids"]) == count
