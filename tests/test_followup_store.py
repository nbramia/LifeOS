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


def test_agent_followup_creation_attaches_a_one_time_scheduler(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEOS_FOLLOWUPS_PATH", str(tmp_path / "followups.json"))

    class FakeSchedule:
        id = "schedule-1"

    class FakeScheduler:
        def __init__(self):
            self.created = []

        def create(self, **kwargs):
            self.created.append(kwargs)
            return FakeSchedule()

    fake = FakeScheduler()
    monkeypatch.setattr("api.services.scheduler_store.get_scheduler_store", lambda: fake)
    from api.services.agent_tools import _tool_manage_followups

    result = _tool_manage_followups({
        "action": "create",
        "person_name": "Sarah",
        "subject": "the proposal",
        "wait_days": 7,
    })
    assert "Conditional follow-up recorded" in result
    assert fake.created[0]["action"] == "prompt"
    assert fake.created[0]["schedule_type"] == "once"
    assert "NO_ACTION" in fake.created[0]["message_content"]
    assert followup_store.list_followups()[0]["schedule_id"] == "schedule-1"
