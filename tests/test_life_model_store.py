from api.services import life_model_store


def test_life_model_records_are_portable_deduplicated_and_source_backed(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEOS_LIFE_MODEL_PATH", str(tmp_path / "life_model.json"))

    first = life_model_store.record(
        "values",
        "I value having time for family",
        source={"type": "telegram", "message_id": "1"},
    )
    second = life_model_store.record(
        "values",
        "  I   value having time for family ",
        source={"type": "telegram", "message_id": "2"},
    )

    rows = life_model_store.list_records("values")
    assert first["id"] == second["id"]
    assert len(rows) == 1
    assert len(rows[0]["sources"]) == 2
    assert rows[0]["evidence_type"] == "explicit"


def test_life_model_rejects_unknown_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEOS_LIFE_MODEL_PATH", str(tmp_path / "life_model.json"))
    try:
        life_model_store.record("preferences", "I like tea")
    except ValueError as exc:
        assert "section" in str(exc)
    else:
        raise AssertionError("unknown section should be rejected")


def test_life_model_source_can_be_attached_after_transport_is_known(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEOS_LIFE_MODEL_PATH", str(tmp_path / "life_model.json"))
    item = life_model_store.record("identity", "a software builder")
    source = {"type": "telegram", "chat_id": "42", "message_id": "9"}
    assert life_model_store.update_source(item["id"], source)
    assert life_model_store.list_records("identity")[0]["sources"] == [source]
