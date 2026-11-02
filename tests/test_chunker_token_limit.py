"""
Tests for the hard token limit every chunk is held to (api/services/chunker.py).

The embedding model's tokenizer is replaced by a small byte-level BPE trained
on synthetic text, so the tests need neither a model download nor the network
and count tokens with a real subword tokenizer. The ceiling is lowered so the
limit is 128 tokens and the inputs stay small.
"""
import random
import string

import pytest

from api.services import chunker
from api.services.chunker import chunk_by_tokens, chunk_document, enforce_token_limit
from config.settings import settings
from tests.tiny_tokenizer import build_tokenizer, text_with_tokens

pytestmark = pytest.mark.unit

CEILING = 384
LIMIT = CEILING - chunker.CONTEXT_PREFIX_RESERVE_TOKENS  # 128 tokens


@pytest.fixture(scope="module")
def tokenizer():
    return build_tokenizer()


@pytest.fixture(autouse=True)
def small_limit(monkeypatch, tokenizer):
    monkeypatch.setattr(settings, "embedding_max_seq_length", CEILING)
    monkeypatch.setattr(chunker, "_embedding_tokenizer", tokenizer)


def tokens(tokenizer, text: str) -> int:
    return len(tokenizer(text, add_special_tokens=False)["input_ids"])


def random_run(n_chars: int, seed: int = 3) -> str:
    return "".join(random.Random(seed).choices(string.ascii_lowercase, k=n_chars))


def assert_all_within_limit(tokenizer, chunks):
    sizes = [tokens(tokenizer, c["content"]) for c in chunks]
    assert max(sizes) <= LIMIT, f"largest chunk {max(sizes)} tokens, limit {LIMIT}"


def assert_covers_exactly(source: str, chunks):
    """Each chunk is a slice of ``source`` and together they leave no gap."""
    covered_to = 0
    for chunk in chunks:
        start = source.find(chunk["content"])
        assert start != -1, "chunk is not a slice of the source"
        assert start <= covered_to, f"gap before offset {start}"
        covered_to = max(covered_to, start + len(chunk["content"]))
    assert covered_to == len(source.rstrip())


class TestLimitHolds:
    def test_long_table(self, tokenizer):
        rows = [f"| row{i:04d} | {i * 7919 % 100003} | https://example.test/p/{i}?q={i * 31} |" for i in range(900)]
        content = "# Ledger\n\n" + "\n".join(rows)
        chunks = chunk_document(content)

        assert len(chunks) > 1
        assert_all_within_limit(tokenizer, chunks)
        joined = " ".join(c["content"] for c in chunks)
        assert all(f"row{i:04d}" in joined for i in range(900))

    def test_long_url(self, tokenizer):
        url = "https://example.test/" + random_run(6000, seed=5)
        chunks = chunk_document(f"See {url} for details.")

        assert len(chunks) > 1
        assert_all_within_limit(tokenizer, chunks)
        assert_covers_exactly(f"See {url} for details.", chunks)

    def test_token_run_without_spaces(self, tokenizer):
        run = random_run(40000)
        assert tokens(tokenizer, run) >= 20000
        chunks = chunk_document(run)

        assert_all_within_limit(tokenizer, chunks)
        assert_covers_exactly(run, chunks)
        assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))

    def test_unicode(self, tokenizer):
        source = " ".join(f"n{i:04d} 日本語のテキスト😀é́ñ العربية русский" for i in range(400))
        chunks = chunk_document(source)

        assert_all_within_limit(tokenizer, chunks)
        assert all("�" not in c["content"] for c in chunks)
        assert_covers_exactly(source, chunks)

    def test_chunk_exactly_at_limit_is_kept_whole(self, tokenizer):
        text = text_with_tokens(tokenizer, LIMIT)
        chunks = enforce_token_limit([{"content": text, "chunk_index": 0}])

        assert [c["content"] for c in chunks] == [text]

    def test_chunk_one_token_over_limit_is_split(self, tokenizer):
        text = text_with_tokens(tokenizer, LIMIT + 1)
        chunks = enforce_token_limit([{"content": text, "chunk_index": 0}])

        assert len(chunks) == 2
        assert_all_within_limit(tokenizer, chunks)
        assert_covers_exactly(text, chunks)


class TestSplitShape:
    def test_pieces_overlap_by_the_requested_tokens(self, tokenizer):
        run = random_run(8000)
        chunks = chunk_document(run, chunk_overlap=20)

        for earlier, later in zip(chunks, chunks[1:]):
            start = run.find(later["content"])
            shared = run[start:run.find(earlier["content"]) + len(earlier["content"])]
            assert 0 < tokens(tokenizer, shared) <= 21

    def test_overlap_larger_than_a_piece_is_capped(self, tokenizer):
        run = random_run(8000)

        for requested in (50, 500):
            chunks = chunk_document(run, chunk_overlap=requested)
            assert_all_within_limit(tokenizer, chunks)
            assert_covers_exactly(run, chunks)
            for earlier, later in zip(chunks, chunks[1:]):
                start = run.find(later["content"])
                shared = run[start:run.find(earlier["content"]) + len(earlier["content"])]
                assert tokens(tokenizer, shared) <= LIMIT // 4 + 1

    def test_text_the_tokenizer_skips_over_is_still_in_the_first_piece(self, monkeypatch):
        real = chunker._token_starts
        monkeypatch.setattr(chunker, "_token_starts", lambda text: [s + 5 for s in real(text[5:])])
        text = "XXXXX" + random_run(3000)

        pieces = chunker._split_to_limit(text, LIMIT, 20)

        assert pieces[0].startswith("XXXXX")

    def test_zero_overlap_pieces_abut(self, tokenizer):
        run = random_run(8000)
        chunks = chunk_document(run, chunk_overlap=0)

        assert "".join(c["content"] for c in chunks) == run

    def test_pieces_are_balanced_not_a_full_piece_plus_remainder(self, tokenizer):
        text = text_with_tokens(tokenizer, LIMIT + 5)
        sizes = [tokens(tokenizer, c["content"]) for c in enforce_token_limit([{"content": text}])]

        assert min(sizes) > LIMIT // 4

    def test_split_pieces_keep_the_section_header(self, tokenizer):
        filler = "\n".join(f"line {i} {random_run(30, seed=i)}" for i in range(120))
        content = f"## Short\n\nsmall body\n\n## Huge\n\n{filler}\n\n## Tail\n\nlast words\n"
        chunks = chunk_document(content, is_meeting=True)

        assert_all_within_limit(tokenizer, chunks)
        huge = [c for c in chunks if c["header"] == "Huge"]
        assert len(huge) > 1
        assert [c["header"] for c in chunks if c["header"] != "Huge"] == ["Short", "Tail"]


class TestOrdinaryNotesUnchanged:
    def test_short_note_is_untouched(self):
        content = "# Title\n\nA short note about nothing in particular.\n"
        chunks = chunk_document(content)

        assert [c["content"] for c in chunks] == [content.strip()]

    def test_header_chunked_meeting_note_is_untouched(self):
        content = "## Agenda\n\nOne two three.\n\n## Actions\n\n- do a thing\n- do another\n"
        chunks = chunk_document(content, is_meeting=True)

        assert [(c["header"], c["content"]) for c in chunks] == [
            ("Agenda", "## Agenda\n\nOne two three."),
            ("Actions", "## Actions\n\n- do a thing\n- do another"),
        ]

    def test_long_note_within_the_limit_matches_word_window_chunking(self, monkeypatch):
        monkeypatch.setattr(settings, "embedding_max_seq_length", 2048)
        rng = random.Random(9)
        body = " ".join("".join(rng.choices(string.ascii_lowercase, k=5)) for _ in range(3000))
        legacy = chunk_by_tokens(body, 500, 50)

        chunks = chunk_document(body)

        assert [c["content"] for c in chunks] == [c["content"] for c in legacy]

    def test_chunks_over_the_target_but_within_the_limit_are_not_split(self, monkeypatch, tokenizer):
        monkeypatch.setattr(settings, "embedding_max_seq_length", 2048)
        words = ["abc12345"] * 384  # one word window, well over 500 tokens
        body = " ".join(words)
        assert 500 < tokens(tokenizer, body) < 2048 - chunker.CONTEXT_PREFIX_RESERVE_TOKENS

        assert [c["content"] for c in chunk_document(body)] == [body]


class TestMeasure:
    def test_without_the_model_tokenizer_the_fallback_still_bounds_chunks(self, monkeypatch):
        monkeypatch.setattr(chunker, "_embedding_tokenizer", None)
        run = random_run(20000)
        chunks = chunk_document(run)

        assert len(chunks) > 1
        assert max(len(chunker._token_starts(c["content"])) for c in chunks) <= LIMIT

    def test_fallback_counts_with_the_same_measure_it_splits_with(self, monkeypatch):
        monkeypatch.setattr(chunker, "_embedding_tokenizer", None)
        if chunker.TOKENIZER is None:
            pytest.skip("tiktoken encoding not available offline")
        text = random_run(LIMIT * 3)  # about 2.3 characters per token: over the limit, but under len // 4
        assert len(text) // 4 <= LIMIT < len(chunker.TOKENIZER.encode(text))

        pieces = [c["content"] for c in enforce_token_limit([{"content": text}])]

        assert len(pieces) > 1
        assert all(len(chunker.TOKENIZER.encode(piece)) <= LIMIT for piece in pieces)

        run = random_run(30000)
        long_pieces = enforce_token_limit([{"content": run}])
        assert all(len(chunker.TOKENIZER.encode(c["content"])) <= LIMIT for c in long_pieces)
        assert_covers_exactly(run, long_pieces)

    def test_with_no_tokenizer_at_all_pieces_follow_the_character_estimate(self, monkeypatch):
        monkeypatch.setattr(chunker, "_embedding_tokenizer", None)
        monkeypatch.setattr(chunker, "TOKENIZER", None)
        text = random_run(5000)

        pieces = [c["content"] for c in enforce_token_limit([{"content": text}])]

        assert len(pieces) > 1
        assert all(len(p) <= LIMIT * chunker.FALLBACK_CHARS_PER_TOKEN for p in pieces)
        assert min(len(p) for p in pieces) > LIMIT  # balanced, not tiny remainders

    def test_a_slow_tokenizer_is_not_used(self, monkeypatch):
        from unittest.mock import MagicMock
        import transformers

        slow = MagicMock(is_fast=False)
        monkeypatch.setattr(transformers.AutoTokenizer, "from_pretrained", MagicMock(return_value=slow))
        monkeypatch.setattr(chunker, "_embedding_tokenizer", chunker._UNLOADED)

        assert chunker._get_embedding_tokenizer() is None

    def test_tokenizer_loads_the_configured_model_from_the_local_cache_only(self, monkeypatch):
        from unittest.mock import MagicMock
        import transformers

        monkeypatch.setattr(settings, "embedding_model", "synthetic/model")
        monkeypatch.setattr(settings, "embedding_cache_dir", "/synthetic/cache")
        loader = MagicMock(side_effect=OSError("not cached"))
        monkeypatch.setattr(transformers.AutoTokenizer, "from_pretrained", loader)
        monkeypatch.setattr(chunker, "_embedding_tokenizer", chunker._UNLOADED)

        assert chunker._get_embedding_tokenizer() is None
        loader.assert_called_once_with(
            "synthetic/model", cache_dir="/synthetic/cache", local_files_only=True
        )
        assert chunker._get_embedding_tokenizer() is None
        assert loader.call_count == 1  # a miss is remembered, not retried per chunk

    def test_an_unset_cache_dir_means_the_default_cache(self, monkeypatch):
        from unittest.mock import MagicMock
        import transformers

        monkeypatch.setattr(settings, "embedding_cache_dir", "")
        loader = MagicMock(return_value=MagicMock(is_fast=True))
        monkeypatch.setattr(transformers.AutoTokenizer, "from_pretrained", loader)
        monkeypatch.setattr(chunker, "_embedding_tokenizer", chunker._UNLOADED)

        chunker._get_embedding_tokenizer()

        assert loader.call_args.kwargs["cache_dir"] is None

    def test_offsets_that_do_not_advance_still_terminate(self, monkeypatch):
        monkeypatch.setattr(chunker, "_token_starts", lambda text: [0] * len(text))
        text = random_run(1000)
        pieces = chunker._split_to_limit(text, LIMIT, 20)

        assert len(pieces) > 1
        assert all(len(p) <= LIMIT for p in pieces)
        assert "".join(pieces) == text


class TestIndexerSeesAllText:
    def test_keyword_index_and_summary_receive_every_row(self, tmp_path, monkeypatch, tokenizer):
        from unittest.mock import MagicMock
        from api.services import indexer as indexer_module
        from api.services.indexer import IndexerService

        rows = [f"| row{i:04d} | {i * 7919 % 100003} | https://example.test/p/{i}?q={i * 31} |" for i in range(900)]
        note = tmp_path / "ledger.md"
        note.write_text("# Ledger\n\n" + "\n".join(rows), encoding="utf-8")

        monkeypatch.setattr(indexer_module, "HAS_V2_PEOPLE", False)
        monkeypatch.setattr(IndexerService, "_invalidate_queued_summary", staticmethod(lambda _path: None))
        summarized = []
        monkeypatch.setattr(
            "api.services.summarizer.generate_summary",
            lambda body, name: (summarized.append(body) or ("a summary", True)),
        )
        indexer = IndexerService(
            vault_path=str(tmp_path),
            connect_stores=False,
            interaction_store=MagicMock(),
            source_entity_store=MagicMock(),
            entity_resolver=MagicMock(),
            tag_store=MagicMock(get=lambda _key: None),
        )
        indexer.vector_store = MagicMock()
        indexer.bm25_index = MagicMock()

        indexer.index_file(str(note))

        embedded, _metadata = indexer.vector_store.update_document.call_args.args
        assert len(embedded) > 1
        assert max(tokens(tokenizer, c["content"]) for c in embedded) <= CEILING
        keyword_text = " ".join(
            call.kwargs["content"] for call in indexer.bm25_index.add_document.call_args_list
            if "::summary" not in call.kwargs["doc_id"]
        )
        assert all(f"row{i:04d}" in keyword_text for i in range(900))
        assert all(f"row{i:04d}" in summarized[0] for i in range(900))
