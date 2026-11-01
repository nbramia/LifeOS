"""scripts/link_slack_entities.py reports its work through SYNC_STATS."""
import json
import re
import sqlite3

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def link_slack(tmp_path, monkeypatch):
    path = tmp_path / "crm.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE source_entities (id TEXT PRIMARY KEY, source_type TEXT, source_id TEXT, "
        "observed_name TEXT, observed_email TEXT, canonical_person_id TEXT)"
    )
    rows = [
        ("g1", "gmail", "g1", "Ada Example", "ada@example.test", "person-ada"),
        ("s1", "slack", "s1", "Ada Example", "ada@example.test", None),
        ("s2", "slack", "s2", "Bo Example", "bo@example.test", None),
        ("s3", "slack", "s3", "Cy Example", "cy@example.test", "person-cy"),
    ]
    conn.executemany("INSERT INTO source_entities VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    import scripts.link_slack_entities as mod

    monkeypatch.setattr(mod, "CRM_DB", path)
    return mod


def _stats(capsys):
    lines = re.findall(r"^SYNC_STATS:(\{.*\})$", capsys.readouterr().out, re.M)
    assert len(lines) == 1
    return json.loads(lines[0])


def test_reports_entities_checked_and_newly_linked(link_slack, capsys):
    link_slack.main(["--execute"])
    assert _stats(capsys) == {"processed": 3, "updated": 1}


def test_idle_night_still_reports_entities_checked(link_slack, capsys):
    link_slack.main(["--execute"])
    capsys.readouterr()
    link_slack.main(["--execute"])
    assert _stats(capsys) == {"processed": 3, "updated": 0}


def test_checked_entities_count_as_orchestrator_yield(link_slack, capsys):
    from scripts.run_all_syncs import _parse_sync_output, _run_yield

    link_slack.main(["--execute"])
    capsys.readouterr()
    link_slack.main(["--execute"])
    assert _run_yield(_parse_sync_output(capsys.readouterr().out)) == 3
