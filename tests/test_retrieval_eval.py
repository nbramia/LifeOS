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


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for rel in [
        "notes/alpha-project-plan.md",
        "notes/beta-meeting-one.md",
        "notes/beta-meeting-two.md",
        ".obsidian/beta-meeting-hidden.md",
        ".trash/alpha-project-old.md",
        "notes/readme.txt",
    ]:
        f = vault / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    return vault


def _read_call(prefix: str, closed: bool = False):
    body = '{"path": "' + prefix + ('"}' if closed else "")
    return {"file_name": f"read_vault_file({body}", "source_type": "vault"}


def test_resolver_unique_ambiguous_nomatch_relative(tmp_path):
    vault = _make_vault(tmp_path)
    r = mine_pairs.VaultResolver(vault)
    # unique absolute prefix; the .trash/ copy of the same stem is excluded
    assert r.resolve(f"{vault}/notes/alpha-pro") == ("match", f"{vault}/notes/alpha-project-plan.md")
    # two files share the prefix; the .obsidian/ copy does not count as a third
    assert r.resolve(f"{vault}/notes/beta-meeting") == ("ambiguous", None)
    assert r.resolve(f"{vault}/notes/gamma") == ("nomatch", None)
    # relative prefix resolves under the vault
    assert r.resolve("notes/beta-meeting-tw") == ("match", f"{vault}/notes/beta-meeting-two.md")
    # hidden and trashed files are not candidates
    assert r.resolve(f"{vault}/.obsidian/beta") == ("nomatch", None)
    assert r.resolve(f"{vault}/.trash/alpha") == ("nomatch", None)
    # a match must start at the beginning of the path, not appear inside it
    assert r.resolve("/notes/alpha-pro") == ("nomatch", None)
    # a bare truncated file name matches on basename
    assert r.resolve("alpha-project") == ("match", f"{vault}/notes/alpha-project-plan.md")
    assert r.resolve("beta-meeting") == ("ambiguous", None)
    # non-.md files are not candidates
    assert r.resolve(f"{vault}/notes/readme") == ("nomatch", None)


def test_files_read_resolves_truncated_and_counts_skips(tmp_path):
    vault = _make_vault(tmp_path)
    stats = mine_pairs.new_stats()
    sources = [
        _read_call(f"{vault}/notes/alpha-pro"),
        _read_call(f"{vault}/notes/beta-meeting"),
        _read_call(f"{vault}/notes/gamma"),
        _read_call("notes/other.md", closed=True),
    ]
    out = mine_pairs.files_read(sources, mine_pairs.VaultResolver(vault), stats)
    assert out == [f"{vault}/notes/alpha-project-plan.md", "notes/other.md"]
    assert stats == {"skipped_truncated_ambiguous": 1, "skipped_truncated_nomatch": 1}


def test_files_read_accepts_filename_key_and_appends_extension(tmp_path):
    vault = _make_vault(tmp_path)
    sources = [
        {"file_name": 'read_vault_file({"filename": "Some Note"})', "source_type": "vault"},
        {"file_name": 'read_vault_file({"filename": "Other Note.md"})', "source_type": "vault"},
        {"file_name": 'read_vault_file({"filename": "alpha-project', "source_type": "vault"},
    ]
    out = mine_pairs.files_read(sources, mine_pairs.VaultResolver(vault), mine_pairs.new_stats())
    assert out == ["Some Note.md", "Other Note.md", f"{vault}/notes/alpha-project-plan.md"]


def test_miner_mined_and_cited_signals(tmp_path):
    vault = _make_vault(tmp_path)
    db = tmp_path / "conversations.db"
    search_call = {"file_name": 'search_vault({"query": "synthetic"})', "source_type": "vault"}
    cited = {"file_name": "beta.md", "file_path": "/vault/notes/beta.md", "source_type": "vault"}
    cited_obsidian = {"file_name": "c.md", "obsidian_path": "notes/c.md", "source_type": "vault"}
    _build_db(db, [
        _user("u1", "c1", "2026-01-01 00:00:01", "find alpha"),
        _assistant("a1", "c1", "2026-01-01 00:00:02", ["search_vault", "read_vault_file"],
                   [search_call, _read_call(f"{vault}/notes/alpha-pro")]),
        # search without a read or citation: no pair
        _user("u2", "c1", "2026-01-01 00:00:03", "find nothing"),
        _assistant("a2", "c1", "2026-01-01 00:00:04", ["search_vault"], [search_call]),
        # read but no search_vault in routing: no mined pair
        _user("u3", "c1", "2026-01-01 00:00:05", "read without search"),
        _assistant("a3", "c1", "2026-01-01 00:00:06", ["read_vault_file"], [_read_call("notes/a.md", closed=True)]),
        # ambiguous truncated path: counted, no pair
        _user("u4", "c2", "2026-01-01 00:00:07", "truncated"),
        _assistant("a4", "c2", "2026-01-01 00:00:08", ["search_vault", "read_vault_file"],
                   [_read_call(f"{vault}/notes/beta-meeting")]),
        # cited files form a separate signal
        _user("u5", "c2", "2026-01-01 00:00:09", "find beta"),
        _assistant("a5", "c2", "2026-01-01 00:00:10", ["search_vault"], [cited, cited_obsidian]),
    ])
    stats = mine_pairs.new_stats()
    pairs = mine_pairs.mine(db, mine_pairs.VaultResolver(vault), stats)
    assert pairs == [
        {"query": "find alpha", "relevant_files": [f"{vault}/notes/alpha-project-plan.md"], "source": "mined"},
        {"query": "find beta", "relevant_files": ["/vault/notes/beta.md", "notes/c.md"], "source": "cited"},
    ]
    assert stats == {"skipped_truncated_ambiguous": 1, "skipped_truncated_nomatch": 0}


def test_main_prints_per_signal_counts_only(tmp_path, capsys):
    vault = _make_vault(tmp_path)
    db = tmp_path / "c.db"
    _build_db(db, [
        _user("u1", "c1", "2026-01-01 00:00:01", "secret synthetic query"),
        _assistant("a1", "c1", "2026-01-01 00:00:02", ["search_vault"],
                   [{"file_name": "b.md", "file_path": "/v/b.md", "source_type": "vault"}]),
    ])
    out = tmp_path / "pairs.jsonl"
    assert mine_pairs.main(["--db", str(db), "--out", str(out), "--vault", str(vault)]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == ("mined=0 cited=1 manual=0 skipped_truncated_ambiguous=0 "
                       "skipped_truncated_nomatch=0 deduped=0 kept=1")
    assert len(out.read_text().splitlines()) == 1


def test_miner_dedupes_repeated_turns_and_records_count(tmp_path, capsys):
    vault = _make_vault(tmp_path)
    db = tmp_path / "c.db"
    cite_a = {"file_name": "a.md", "file_path": "/v/a.md", "source_type": "vault"}
    cite_b = {"file_name": "b.md", "file_path": "/v/b.md", "source_type": "vault"}
    _build_db(db, [
        _user("u1", "c1", "2026-01-01 00:00:01", "Where is the plan"),
        _assistant("a1", "c1", "2026-01-01 00:00:02", ["search_vault"], [cite_a]),
        # same query modulo case and whitespace, same files: merged
        _user("u2", "c1", "2026-01-01 00:00:03", "  where is the PLAN "),
        _assistant("a2", "c1", "2026-01-01 00:00:04", ["search_vault"], [cite_a]),
        # same query, different files: kept separate
        _user("u3", "c2", "2026-01-01 00:00:05", "where is the plan"),
        _assistant("a3", "c2", "2026-01-01 00:00:06", ["search_vault"], [cite_b]),
        _user("u4", "c2", "2026-01-01 00:00:07", "where is the plan"),
        _assistant("a4", "c2", "2026-01-01 00:00:08", ["search_vault"], [cite_a]),
    ])
    out = tmp_path / "pairs.jsonl"
    assert mine_pairs.main(["--db", str(db), "--out", str(out), "--vault", str(vault)]) == 0
    recs = [json.loads(line) for line in out.read_text().splitlines()]
    assert [(r["relevant_files"], r["count"]) for r in recs] == [(["/v/a.md"], 3), (["/v/b.md"], 1)]
    assert "deduped=2 kept=2" in capsys.readouterr().out


def _score_main(monkeypatch, capsys, tmp_path, *extra):
    import score

    ranked = {"q hot": ["x.md", "a.md"], "q cold": ["y.md"]}
    monkeypatch.setattr(score, "make_searcher", lambda arm, db: (lambda q, k: ranked[q]))
    pairs = tmp_path / "p.jsonl"
    pairs.write_text("\n".join(json.dumps(r) for r in [
        {"query": "q hot", "relevant_files": ["a.md"], "source": "mined", "count": 3},
        {"query": "q cold", "relevant_files": ["b.md"], "source": "mined"},
    ]))
    assert score.main(["--arm", "bm25", "--pairs", str(pairs), *extra]) == 0
    return capsys.readouterr().out


def test_score_default_counts_each_record_once(monkeypatch, capsys, tmp_path):
    # q hot: a.md at rank 2 -> recall@10 1, RR 1/2. q cold: b.md absent -> 0, 0.
    # Unweighted: recall@10 = (1 + 0) / 2 = 0.5; MRR = (1/2 + 0) / 2 = 0.25.
    out = _score_main(monkeypatch, capsys, tmp_path)
    assert "n=2" in out and "weight=" not in out
    assert "recall@10=0.500" in out and "mrr=0.250" in out


def test_score_weighted_uses_count(monkeypatch, capsys, tmp_path):
    # Weights 3 (count=3) and 1 (count absent). Total weight 4.
    # recall@10 = (3*1 + 1*0) / 4 = 0.75; MRR = (3*1/2 + 0) / 4 = 0.375.
    out = _score_main(monkeypatch, capsys, tmp_path, "--weighted")
    assert "n=2" in out and "weight=4" in out
    assert "recall@10=0.750" in out and "mrr=0.375" in out


def test_filter_pairs_excludes_sources():
    pairs = [{"source": "mined"}, {"source": "cited"}, {"source": "manual"}, {"source": "cited"}]
    assert _match.filter_pairs(pairs, ["cited"]) == [{"source": "mined"}, {"source": "manual"}]
    assert _match.filter_pairs(pairs, ["cited", "mined"]) == [{"source": "manual"}]
    assert _match.filter_pairs(pairs) == pairs


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
