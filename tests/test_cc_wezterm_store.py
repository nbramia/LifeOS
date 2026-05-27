"""Unit tests for the cc_wezterm_store SQLite mapping module."""
from __future__ import annotations

from pathlib import Path

import pytest

from api.services.cc_wezterm_store import CCWezTermStore


@pytest.fixture
def store(tmp_path: Path) -> CCWezTermStore:
    return CCWezTermStore(db_path=tmp_path / "cc_wezterm.db")


@pytest.mark.unit
def test_get_returns_none_when_missing(store):
    assert store.get("cc:nonexistent") is None


@pytest.mark.unit
def test_upsert_then_get_returns_mapping(store):
    m = store.upsert("cc:abc-123", pane_id=7, cwd="/tmp/proj")
    assert m.session_id == "cc:abc-123"
    assert m.pane_id == 7
    assert m.cwd == "/tmp/proj"

    fetched = store.get("cc:abc-123")
    assert fetched is not None
    assert fetched.pane_id == 7
    assert fetched.cwd == "/tmp/proj"
    assert fetched.created_at > 0


@pytest.mark.unit
def test_upsert_overwrites_existing_pane_id(store):
    store.upsert("cc:abc-123", pane_id=7, cwd="/tmp/proj")
    store.upsert("cc:abc-123", pane_id=42, cwd="/tmp/proj-renamed")
    m = store.get("cc:abc-123")
    assert m.pane_id == 42
    assert m.cwd == "/tmp/proj-renamed"


@pytest.mark.unit
def test_delete_removes_mapping(store):
    store.upsert("cc:abc-123", pane_id=7, cwd="/tmp/proj")
    assert store.delete("cc:abc-123") is True
    assert store.get("cc:abc-123") is None
    # Deleting again is a no-op.
    assert store.delete("cc:abc-123") is False


@pytest.mark.unit
def test_db_persists_across_instances(tmp_path):
    db = tmp_path / "cc_wezterm.db"
    s1 = CCWezTermStore(db_path=db)
    s1.upsert("cc:x", pane_id=9, cwd="/a")
    s1.close()

    s2 = CCWezTermStore(db_path=db)
    m = s2.get("cc:x")
    assert m is not None
    assert m.pane_id == 9
    s2.close()


@pytest.mark.unit
def test_db_path_parent_is_created(tmp_path):
    nested = tmp_path / "nested" / "deeper" / "cc_wezterm.db"
    assert not nested.parent.exists()
    s = CCWezTermStore(db_path=nested)
    assert nested.parent.exists()
    s.close()
