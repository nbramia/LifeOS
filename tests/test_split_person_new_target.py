"""Splitting sources onto a brand-new person (`--to-person NEW`, executed)."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "split_person.py"


@pytest.fixture
def split_person():
    spec = importlib.util.spec_from_file_location("split_person_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_executed_split_to_a_new_person_creates_and_fills_it(split_person, monkeypatch):
    source = SimpleNamespace(id="from-1", canonical_name="Synthetic Source", relationship_strength=1.0)
    saved = {}
    store = MagicMock()
    store.add.side_effect = lambda person: saved.setdefault("person", person)
    store.get_by_id.side_effect = lambda pid: saved.get("person") if pid != "from-1" else source

    monkeypatch.setattr(split_person, "find_person_by_name", lambda name: source)
    monkeypatch.setattr(split_person, "get_person_entity_store", lambda: store)
    monkeypatch.setattr(split_person, "get_source_entities_for_person", lambda *a, **k: [])
    monkeypatch.setattr(split_person, "move_source_entities", lambda *a, **k: 0)
    monkeypatch.setattr(split_person, "move_interactions", lambda *a, **k: 0)
    monkeypatch.setattr(split_person, "update_person_sources", lambda pid: [])
    monkeypatch.setattr(split_person, "create_link_overrides", lambda *a, **k: 0)
    monkeypatch.setattr("api.services.person_stats.refresh_person_stats", lambda ids, **k: None)
    monkeypatch.setattr("api.services.relationship_metrics.update_strength_for_person", lambda pid: None)

    stats = split_person.split_person(
        "Synthetic Source", "NEW", ["vault"], new_person_name="Synthetic Target", dry_run=False
    )

    assert saved["person"].canonical_name == "Synthetic Target"
    assert stats["to_person"] == {"id": saved["person"].id, "name": "Synthetic Target"}
