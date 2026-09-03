"""
Tests for api/services/atomic_write.py

The shared atomic-write helper used by both task_manager.py and
scheduler_store.py: temp file in the same directory, fsync, os.replace.
"""
import builtins
import unittest.mock as mock
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


def test_destination_is_never_opened_directly_for_writing(tmp_path):
    """A reader opening `target` mid-write must see either the old content
    in full or the new content in full — never a partial write. Proven here
    by showing the destination path itself is never opened in a write mode;
    only a temp file is, and the swap happens via a single os.replace."""
    target = tmp_path / "file.txt"
    target.write_text("old", encoding="utf-8")

    opened_for_write = []
    original_open = builtins.open

    def spy_open(file, mode="r", *args, **kwargs):
        if any(c in mode for c in ("w", "a", "x")):
            opened_for_write.append(str(file))
        return original_open(file, mode, *args, **kwargs)

    with mock.patch("builtins.open", spy_open):
        atomic_write_text(target, "new content")

    assert str(target) not in opened_for_write
    assert target.read_text(encoding="utf-8") == "new content"


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
