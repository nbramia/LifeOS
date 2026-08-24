import json

from api.services import inbox_store


def test_update_item_retains_pending_proposal(tmp_path, monkeypatch):
    path = tmp_path / "inbox.json"
    monkeypatch.setenv("LIFEOS_INBOX_PATH", str(path))

    item = inbox_store.add_item("Remind me to call John next week", source={"type": "telegram"})
    updated = inbox_store.update_item(
        item["id"],
        status="processed",
        category="reminder",
        proposal={
            "type": "reminder",
            "content": "Remind me to call John next week",
            "requires_confirmation": True,
        },
    )

    assert updated["status"] == "processed"
    assert updated["proposal"]["requires_confirmation"] is True
    persisted = json.loads(path.read_text())
    assert persisted["items"][0]["source"] == {"type": "telegram"}
    assert persisted["items"][0]["proposal"]["type"] == "reminder"
