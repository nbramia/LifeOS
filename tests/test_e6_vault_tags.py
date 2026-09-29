"""Smoke test for scripts/jev_eval/e6_vault_tags.py (synthetic vault, mocked Jev)."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from api.services.jev_client import JevClient
from api.services.vault_taxonomy import load_taxonomy
from config.settings import settings

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "jev_eval"))
import e6_vault_tags  # noqa: E402

TX = load_taxonomy()


def _answers(*_a, **_k):
    def choice(opt):
        return {"choice": opt, "probabilities": {opt: 0.9}, "confidence": 0.9}

    return {
        "doc_type": choice("meeting_notes"),
        "domain": choice("work"),
        "topic": choice(next(iter(TX.topics))),
        "project": choice("none"),
        "actionability": {"score": 1.0},
        "has_decision": {"noul": 0.2},
    }


def test_e6_prints_aggregates_and_writes_only_out(tmp_path, monkeypatch, capsys):
    tmp_path = tmp_path / "e6"
    vault = tmp_path / "vault"
    (vault / "Notes").mkdir(parents=True)
    (vault / "Notes" / "a.md").write_text("---\ntype: meeting\ntags: [work]\n---\n# Alpha\nSECRET-BODY-TEXT\n")
    (vault / "Notes" / "b.md").write_text("# Beta\nMore SECRET-BODY-TEXT.\n")
    gold = tmp_path / "gold.jsonl"
    gold.write_text(json.dumps({"file_path": "Notes/a.md", "doc_type": "meeting_notes"}) + "\n")
    out = tmp_path / "out" / "results.json"
    default_out = REPO_ROOT / "data" / "jev_eval" / "e6_results.json"
    existed = default_out.exists()

    monkeypatch.setattr(settings, "vault_path", vault)
    monkeypatch.setattr(settings, "typesafe_api_key", "k")
    monkeypatch.setattr(settings, "jev_vault_tagging", "off")
    monkeypatch.setattr(settings, "jev_vault_tag_paths", "")
    monkeypatch.setattr(sys, "argv", ["e6", "--folder", "Notes", "--gold", str(gold), "--out", str(out)])
    with patch.object(JevClient, "ask", side_effect=_answers):
        e6_vault_tags.main()

    printed = capsys.readouterr().out
    assert "SECRET-BODY-TEXT" not in printed and "Notes/a.md" not in printed
    report = json.loads(printed)
    assert report["notes"] == 2 and report["tagged_by_jev"] == 2
    assert report["silver_agreement"]["doc_type"] == {"n": 1, "agreement": 1.0}
    assert report["gold_accuracy"]["doc_type"] == {"n": 1, "agreement": 1.0}
    assert report["calibration"]["doc_type"][0]["bucket"] == "0.9-1.0"
    assert report["flip_rate"]["doc_type"] == 0.0
    assert json.loads(out.read_text()) == report
    assert sorted(p.name for p in tmp_path.iterdir()) == ["gold.jsonl", "out", "vault"]
    assert default_out.exists() == existed
