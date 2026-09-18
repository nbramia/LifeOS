"""The background PR-status refresher (api/services/pr_status_prefetch.py)
that keeps `session_store.pr_status_cache` current so a board read never
calls `gh` on the request path.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from api.services import pr_status_prefetch
from api.services.agent_worker import session_store as session_store_module
from api.services.agent_worker.session_store import SessionStore

pytestmark = pytest.mark.unit

PR_URL = "https://github.com/nbramia/LifeOS/pull/1234"
PR_URL_2 = "https://github.com/nbramia/LifeOS/pull/5678"


def _store(tmp_path: Path) -> SessionStore:
    return SessionStore(db_path=tmp_path / "sessions.db")


def test_gh_pr_view_normalizes_a_successful_call(monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd[:3] == ["gh", "pr", "view"]
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout='{"number": 1234, "title": "Add outcome", "state": "MERGED", "mergedAt": "2026-09-18T04:00:00Z"}',
            stderr="",
        )
    monkeypatch.setattr(subprocess, "run", fake_run)
    info = pr_status_prefetch._gh_pr_view(PR_URL)
    assert info == {
        "number": 1234, "title": "Add outcome", "state": "MERGED",
        "merged_at": "2026-09-18T04:00:00Z",
    }


def test_gh_pr_view_returns_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found"),
    )
    assert pr_status_prefetch._gh_pr_view(PR_URL) is None


def test_gh_pr_view_returns_none_on_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 15))
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert pr_status_prefetch._gh_pr_view(PR_URL) is None


def test_refresh_stale_writes_results_for_every_stale_url(tmp_path: Path):
    store = _store(tmp_path)
    store.record_card_outcome(
        "task-1", session_id="s1", engine_label="Claude Code",
        summary="a", branch=None, pr_urls=[PR_URL, PR_URL_2],
    )
    calls: list[str] = []

    def viewer(url):
        calls.append(url)
        if url == PR_URL:
            return {"number": 1234, "title": "t", "state": "OPEN", "merged_at": None}
        return None  # simulates a lookup failure for the second PR

    refreshed = pr_status_prefetch.refresh_stale(store, viewer=viewer)

    assert refreshed == 2
    assert sorted(calls) == sorted([PR_URL, PR_URL_2])
    assert store.get_pr_status(PR_URL)["state"] == "OPEN"
    assert store.get_pr_status(PR_URL)["stale"] is False
    status_2 = store.get_pr_status(PR_URL_2)
    assert status_2["state"] is None
    assert status_2["stale"] is True


def test_refresh_stale_never_touches_a_fresh_entry(tmp_path: Path):
    store = _store(tmp_path)
    store.record_card_outcome(
        "task-1", session_id="s1", engine_label="Claude Code",
        summary="a", branch=None, pr_urls=[PR_URL],
    )
    store.upsert_pr_status(PR_URL, {"number": 1234, "title": "t", "state": "OPEN", "merged_at": None})

    calls: list[str] = []
    refreshed = pr_status_prefetch.refresh_stale(
        store, ttl_s=300, viewer=lambda url: calls.append(url) or None,
    )

    assert refreshed == 0
    assert calls == []
    assert store.get_pr_status(PR_URL)["state"] == "OPEN"


def test_refresh_stale_bounds_the_number_of_urls_per_call(tmp_path: Path):
    store = _store(tmp_path)
    urls = [f"https://github.com/o/r/pull/{n}" for n in range(5)]
    store.record_card_outcome(
        "task-1", session_id="s1", engine_label="Claude Code",
        summary="a", branch=None, pr_urls=urls,
    )
    calls: list[str] = []
    refreshed = pr_status_prefetch.refresh_stale(
        store, max_refresh=2, viewer=lambda url: calls.append(url) or None,
    )
    assert refreshed == 2
    assert len(calls) == 2


def test_a_pr_open_at_completion_reads_merged_after_a_later_refresh(tmp_path: Path):
    """The freshness mechanism end to end: a PR that starts open and is
    later reported merged reflects that on the next refresh, not the value
    recorded when the card's outcome was first written."""
    store = _store(tmp_path)
    store.record_card_outcome(
        "task-1", session_id="s1", engine_label="Claude Code",
        summary="a", branch=None, pr_urls=[PR_URL],
    )
    pr_status_prefetch.refresh_stale(
        store,
        viewer=lambda url: {"number": 1234, "title": "t", "state": "OPEN", "merged_at": None},
    )
    assert store.get_pr_status(PR_URL)["state"] == "OPEN"

    # A negative ttl_s forces the entry to be picked up again immediately
    # regardless of clock resolution, standing in for "the next tick after
    # the TTL elapses" without a real sleep.
    pr_status_prefetch.refresh_stale(
        store, ttl_s=-1,
        viewer=lambda url: {
            "number": 1234, "title": "t", "state": "MERGED",
            "merged_at": "2026-09-18T04:00:00Z",
        },
    )
    status = store.get_pr_status(PR_URL)
    assert status["state"] == "MERGED" and status["stale"] is False


def test_refresh_stale_eventually_covers_every_url_without_starving_the_tail(tmp_path: Path):
    """Reproduces the starvation failure mode directly: more urls than
    `ttl_s / tick × max_refresh` would ever reach in the naive
    first-N-in-table-order selection. Fair ordering (never-checked first,
    then oldest checked_at) guarantees every url is attempted exactly
    once across enough ticks, with no repeats crowding out the tail."""
    store = _store(tmp_path)
    urls = [f"https://github.com/o/r/pull/{n}" for n in range(40)]
    store.record_card_outcome(
        "task-1", session_id="s1", engine_label="Claude Code",
        summary="a", branch=None, pr_urls=urls,
    )
    max_refresh = 3
    ticks = -(-len(urls) // max_refresh)  # ceil(40 / 3) == 14

    attempted: list[str] = []

    def viewer(url):
        attempted.append(url)
        return {"number": 1, "title": "t", "state": "OPEN", "merged_at": None}

    # A large ttl_s means a url just refreshed this run never re-qualifies
    # as stale within the loop — isolating the ordering guarantee from
    # TTL expiry, which is covered by the other freshness tests above.
    for _ in range(ticks):
        pr_status_prefetch.refresh_stale(store, ttl_s=300, max_refresh=max_refresh, viewer=viewer)

    assert len(attempted) == len(urls)
    assert set(attempted) == set(urls)


def test_refresh_stale_does_not_permanently_starve_the_tail_once_entries_recur(
    tmp_path: Path, monkeypatch,
):
    """Reproduces the exact production failure mode: entries near the
    front of the outcome table go stale again (real TTL expiry, real
    ticking) before the scan ever reaches the tail. Mirrors the review's
    own repro shape — 40 PRs, a 3-per-tick batch, a TTL shorter than a
    full pass needs — and asserts every url gets attempted at least once
    within that many ticks, not just the front third of the table."""
    clock = [1_000_000]
    monkeypatch.setattr(session_store_module, "_now", lambda: clock[0])
    store = _store(tmp_path)
    urls = [f"https://github.com/o/r/pull/{n}" for n in range(40)]
    store.record_card_outcome(
        "task-1", session_id="s1", engine_label="Claude Code",
        summary="a", branch=None, pr_urls=urls,
    )

    attempts: dict[str, int] = {url: 0 for url in urls}

    def viewer(url):
        attempts[url] += 1
        return {"number": 1, "title": "t", "state": "OPEN", "merged_at": None}

    tick_seconds = 30
    ttl_s = 300  # a url refreshed at tick N is stale again by tick N+10
    for _ in range(30):
        pr_status_prefetch.refresh_stale(store, ttl_s=ttl_s, max_refresh=3, viewer=viewer)
        clock[0] += tick_seconds

    never_attempted = [url for url, count in attempts.items() if count == 0]
    assert never_attempted == [], (
        f"{len(never_attempted)} of {len(urls)} urls were never refreshed — "
        "the front of the table is starving the tail"
    )
