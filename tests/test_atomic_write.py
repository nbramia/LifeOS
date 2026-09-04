"""
Tests for api/services/atomic_write.py

The shared atomic-write helper used by both task_manager.py and
scheduler_store.py: temp file in the same directory, fsync, os.replace.
"""
import pytest
from pathlib import Path

from api.services.atomic_write import atomic_write_text, atomic_write_lines

pytestmark = pytest.mark.unit


def test_atomic_write_text_creates_file(tmp_path):
    target = tmp_path / "sub" / "file.txt"
    atomic_write_text(target, "hello world")
    assert target.read_text(encoding="utf-8") == "hello world"


def test_atomic_write_text_overwrites_existing_content(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_text_no_leftover_temp_file(tmp_path):
    target = tmp_path / "file.txt"
    atomic_write_text(target, "content")
    assert list(tmp_path.glob(".*.tmp")) == []


def test_atomic_write_lines_joins_with_trailing_newline(tmp_path):
    target = tmp_path / "file.txt"
    atomic_write_lines(target, ["a", "b", "c"])
    assert target.read_text(encoding="utf-8") == "a\nb\nc\n"


def test_atomic_write_uses_replace_with_temp_file_in_same_dir(tmp_path, monkeypatch):
    import api.services.atomic_write as aw

    calls = []
    original = aw.os.replace

    def spy(src, dst):
        calls.append((src, dst))
        return original(src, dst)

    monkeypatch.setattr(aw.os, "replace", spy)

    target = tmp_path / "file.txt"
    atomic_write_text(target, "data")

    assert len(calls) == 1
    src, dst = calls[0]
    assert Path(src).parent == target.parent
    assert dst == str(target)


def test_atomic_write_lines_leaves_destination_unchanged_on_replace_failure(tmp_path, monkeypatch):
    """Same atomic-semantics proof as
    `test_failed_replace_cleans_up_temp_file_and_leaves_original` below, for
    `atomic_write_lines` specifically: if `os.replace` fails mid-write, the
    destination keeps its old content in full and no temp file survives —
    never a partial write. (The prior version of this test spied on
    `builtins.open`, but `os.fdopen` routes through `io.open` regardless of
    write path, so it passed even against a non-atomic `path.write_text` —
    it proved nothing about atomicity.)"""
    import api.services.atomic_write as aw

    target = tmp_path / "file.txt"
    target.write_text("old\n", encoding="utf-8")

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(aw.os, "replace", boom)

    with pytest.raises(OSError):
        atomic_write_lines(target, ["new", "lines"])

    assert target.read_text(encoding="utf-8") == "old\n"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_failed_replace_cleans_up_temp_file_and_leaves_original(tmp_path, monkeypatch):
    import api.services.atomic_write as aw

    def boom(src, dst):
        raise OSError("simulated failure")

    monkeypatch.setattr(aw.os, "replace", boom)

    target = tmp_path / "file.txt"
    target.write_text("original", encoding="utf-8")

    with pytest.raises(OSError):
        atomic_write_text(target, "new data")

    assert list(tmp_path.glob(".*.tmp")) == []
    assert target.read_text(encoding="utf-8") == "original"
