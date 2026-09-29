"""Retrieval eval tooling: scorer math on a synthetic fixture and the pair miner."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "retrieval_eval"))

import _match  # noqa: E402
import mine_pairs  # noqa: E402

pytestmark = pytest.mark.unit

FIXTURES = ROOT / "tests" / "fixtures" / "retrieval_eval"


@pytest.fixture
def rankings():
    ranked = json.loads((FIXTURES / "ranked.json").read_text())
    pairs = [json.loads(line) for line in (FIXTURES / "pairs.jsonl").read_text().splitlines()]
    return [(ranked[p["query"]], p["relevant_files"]) for p in pairs]


def test_normalize_strips_chunk_suffixes_and_path():
    assert _match.normalize_file("vault/Notes/Alpha.md::3") == "alpha.md"
    assert _match.normalize_file("vault/alpha.md_12") == "alpha.md"
    assert _match.normalize_file("alpha.md") == "alpha.md"


def test_known_recall_and_mrr(rankings):
    # q1: relevant alpha; distinct ranking beta, alpha -> rank 2. recall 1, RR 1/2.
    # q2: relevant gamma, delta; the raw ranking repeats gamma, so distinct files are
    #     gamma (1), 8 fillers (2-9), delta (10): recall@10 = 2/2, RR = 1/1.
    #     Without chunk dedupe delta sits at raw position 11 and misses the top 10.
    # q3: relevant epsilon at distinct rank 12: recall@10 = 0, recall@40 = 1, RR = 1/12.
    # recall@10 = (1 + 1 + 0) / 3 = 2/3
    # recall@40 = (1 + 1 + 1) / 3 = 1
    # MRR       = (1/2 + 1 + 1/12) / 3 = (19/12) / 3 = 19/36
    res = _match.score_queries(rankings, k=10, k_wide=40)
    assert res["n"] == 3
    assert res["recall@10"] == pytest.approx(2 / 3)
    assert res["recall@40"] == pytest.approx(1.0)
    assert res["mrr"] == pytest.approx(19 / 36)


def test_small_k_changes_recall(rankings):
    # k=1: q1 top file is beta -> 0; q2 top file gamma -> 1/2; q3 -> 0. Mean = 0.5 / 3 = 1/6.
    assert _match.score_queries(rankings, k=1, k_wide=40)["recall@1"] == pytest.approx(1 / 6)


def test_empty_rankings():
    assert _match.score_queries([])["mrr"] == 0.0


def _build_db(path: Path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            sources TEXT,
            routing TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.executemany(
        "INSERT INTO messages (id, conversation_id, role, content, sources, routing, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _assistant(mid, conv, ts, tools, sources):
    return (mid, conv, "assistant", "reply", json.dumps(sources), json.dumps({"sources": tools, "reasoning": "x", "tool_rounds": len(tools)}), ts)


def _user(mid, conv, ts, text):
    return (mid, conv, "user", text, None, None, ts)


def test_miner_extracts_read_files_from_synthetic_db(tmp_path):
    db = tmp_path / "conversations.db"
    read_alpha = {"file_name": 'read_vault_file({"path": "notes/alpha.md"})', "source_type": "vault"}
    read_truncated = {"file_name": 'read_vault_file({"path": "notes/a-very-long-dire', "source_type": "vault"}
    search_call = {"file_name": 'search_vault({"query": "synthetic"})', "source_type": "vault"}
    file_path_entry = {"file_name": "beta.md", "file_path": "/vault/notes/beta.md", "source_type": "vault"}
    _build_db(db, [
        _user("u1", "c1", "2026-01-01 00:00:01", "find alpha"),
        _assistant("a1", "c1", "2026-01-01 00:00:02", ["search_vault", "read_vault_file"], [search_call, read_alpha]),
        # search without a read: no pair
        _user("u2", "c1", "2026-01-01 00:00:03", "find nothing"),
        _assistant("a2", "c1", "2026-01-01 00:00:04", ["search_vault"], [search_call]),
        # read but no search_vault in routing: no pair
        _user("u3", "c1", "2026-01-01 00:00:05", "read without search"),
        _assistant("a3", "c1", "2026-01-01 00:00:06", ["read_vault_file"], [read_alpha]),
        # truncated path is unrecoverable: no pair
        _user("u4", "c2", "2026-01-01 00:00:07", "truncated"),
        _assistant("a4", "c2", "2026-01-01 00:00:08", ["search_vault", "read_vault_file"], [read_truncated]),
        # explicit file_path entry is used
        _user("u5", "c2", "2026-01-01 00:00:09", "find beta"),
        _assistant("a5", "c2", "2026-01-01 00:00:10", ["search_vault"], [file_path_entry]),
    ])
    pairs = mine_pairs.mine(db)
    assert pairs == [
        {"query": "find alpha", "relevant_files": ["notes/alpha.md"], "source": "mined"},
        {"query": "find beta", "relevant_files": ["/vault/notes/beta.md"], "source": "mined"},
    ]


def test_manual_pairs_appended_and_no_output_of_content(tmp_path, capsys):
    db = tmp_path / "c.db"
    _build_db(db, [])
    manual = tmp_path / "manual.jsonl"
    manual.write_text(json.dumps({"query": "synthetic manual", "relevant_files": ["notes/x.md"]}) + "\n")
    out = tmp_path / "out" / "pairs.jsonl"
    assert mine_pairs.main(["--db", str(db), "--out", str(out), "--manual", str(manual)]) == 0
    recs = [json.loads(line) for line in out.read_text().splitlines()]
    assert recs == [{"query": "synthetic manual", "relevant_files": ["notes/x.md"], "source": "manual"}]
    assert "synthetic manual" not in capsys.readouterr().out
