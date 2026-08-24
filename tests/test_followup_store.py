from api.services import followup_store


def test_conditional_followup_is_idempotent_and_queryable(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEOS_FOLLOWUPS_PATH", str(tmp_path / "followups.json"))
    first = followup_store.create(
        "Sarah",
        "the proposal",
        wait_days=7,
        source={"type": "telegram", "message_id": "10"},
    )
    second = followup_store.create("Sarah", "the proposal", wait_days=7)
    assert first["id"] == second["id"]
    assert first["condition"] == "no_response"
    assert first["check_at"] > first["created_at"]
    assert len(followup_store.list_followups()) == 1


def test_conditional_followup_can_be_completed(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEOS_FOLLOWUPS_PATH", str(tmp_path / "followups.json"))
    item = followup_store.create("John", "the deck")
    completed = followup_store.update_status(item["id"], "completed")
    assert completed["status"] == "completed"
    assert followup_store.list_followups() == []
