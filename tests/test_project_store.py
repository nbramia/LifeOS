from api.services import project_store


def test_project_upsert_preserves_history_and_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEOS_PROJECTS_PATH", str(tmp_path / "projects.json"))
    first = project_store.upsert(
        "Cafe AI",
        status="potential",
        summary="A management product for cafes",
        source={"type": "telegram", "message_id": "1"},
    )
    second = project_store.upsert(
        " cafe   ai ",
        status="active",
        next_action="Map the first workflow",
        source={"type": "telegram", "message_id": "2"},
    )

    assert first["id"] == second["id"]
    current = project_store.get_project(name="Cafe AI")
    assert current["status"] == "active"
    assert current["next_action"] == "Map the first workflow"
    assert len(current["history"]) == 1
    assert len(current["sources"]) == 2


def test_project_list_hides_archived_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEOS_PROJECTS_PATH", str(tmp_path / "projects.json"))
    item = project_store.upsert("Old project", status="active")
    project_store.upsert("Old project", status="archived")
    assert project_store.list_projects() == []
    assert project_store.list_projects(include_archived=True)[0]["id"] == item["id"]
