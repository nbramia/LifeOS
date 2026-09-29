"""Tests for scripts/search_attribution_report.py over a synthetic trace db."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import search_attribution_report as report  # noqa: E402

from api.services.perf_trace import PerfTraceStore  # noqa: E402


def _span(query, vec, bm25, mode, attribution, top_k):
    return {
        "name": "search_attribution", "duration_ms": 0.1, "parent": "tool_search_vault",
        "metadata": {
            "query": query, "vector_candidates": vec, "bm25_candidates": bm25,
            "bm25_match_mode": mode, "attribution": attribution, "top_k": top_k,
        },
    }


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "perf_traces.db")
    PerfTraceStore(path)  # creates the real schema
    conn = sqlite3.connect(path)
    rows = [
        ("t1", "2026-09-10T10:00:00", [
            _span("secret alpha", 10, 5, {"and": 5}, {"vector_only": 2, "bm25_only": 1, "both": 1}, 4),
            {"name": "search_vector", "duration_ms": 1, "parent": None, "metadata": {}},
        ]),
        ("t2", "2026-09-11T10:00:00", [
            _span("secret beta", 10, 0, "none", {"vector_only": 4, "bm25_only": 0, "both": 0}, 4)]),
        ("t3", "2026-09-12T10:00:00", [
            _span("secret gamma", 0, 3, {"and": 1, "or": 2}, {"vector_only": 0, "bm25_only": 2, "both": 0}, 2)]),
        ("old", "2026-01-01T10:00:00", [
            _span("secret old", 1, 1, "and", {"vector_only": 0, "bm25_only": 0, "both": 1}, 1)]),
        ("plain", "2026-09-13T10:00:00", [
            {"name": "x", "duration_ms": 1, "parent": None, "metadata": {}}]),
    ]
    for tid, created, spans in rows:
        conn.execute(
            "INSERT INTO traces VALUES (?, 'c', 'q', 'm', 1.0, ?, ?)",
            (tid, created, json.dumps(spans)),
        )
    conn.commit()
    conn.close()
    return path


def test_report_aggregates_searches_since_date(db, capsys):
    assert report.main(["--since", "2026-09-01", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "Searches counted: 3" in out
    # BM25-reachable shares are 0.5, 0.0, 1.0
    assert "mean 50.0%, median 50.0%" in out
    assert "zero BM25 candidates: 33.3% (1/3)" in out
    assert "zero vector candidates: 33.3% (1/3)" in out
    assert "and=1, and+or=1, none=1" in out
    assert "top_k distribution: 2=1, 4=2" in out


def test_report_hides_queries_by_default(db, capsys):
    report.main(["--since", "2026-09-01", "--db", db])
    assert "secret" not in capsys.readouterr().out


def test_report_shows_queries_when_asked(db, capsys):
    report.main(["--since", "2026-09-01", "--db", db, "--show-queries"])
    assert "secret alpha" in capsys.readouterr().out


def test_report_opens_db_read_only(db, monkeypatch):
    real_connect = sqlite3.connect
    seen = {}

    def spy(target, *a, **kw):
        seen["target"], seen["uri"] = target, kw.get("uri")
        return real_connect(target, *a, **kw)

    monkeypatch.setattr(report.sqlite3, "connect", spy)
    report.load_attributions(db, "2026-09-01")
    assert seen["uri"] is True and seen["target"].endswith("?mode=ro")


def test_report_with_no_searches(tmp_path, capsys):
    path = str(tmp_path / "e.db")
    PerfTraceStore(path)
    assert report.main(["--since", "2026-09-01", "--db", path]) == 0
    assert "Searches counted: 0" in capsys.readouterr().out
