"""Tests for scripts/photos_origin_helper.py, the read-only Photos library
reader that runs on the Photos Mac over SSH.

The helper runs as a subprocess against a synthetic library in a temp HOME,
with a stub ``sips`` first on PATH.
"""
import ast
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests._photos_fakes import (
    MASTERS, MEDIUM, SMALL, UUID_A, UUID_B, UUID_C, UUID_UNKNOWN,
    UUID_VIDEO, make_library, standard_assets,
)

pytestmark = pytest.mark.unit

HELPER = Path(__file__).resolve().parent.parent / "scripts" / "photos_origin_helper.py"

EXIT_OK, EXIT_INTERNAL, EXIT_USAGE, EXIT_NOT_FOUND, EXIT_UNAVAILABLE = 0, 1, 2, 3, 4


@pytest.fixture
def home(tmp_path):
    """A temp HOME with a synthetic library and a recording `sips` stub."""
    make_library(tmp_path, standard_assets())

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "sips"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "out = args[args.index('--out') + 1]\n"
        "with open(os.environ['SIPS_LOG'], 'a') as log:\n"
        "    log.write(' '.join(args) + '\\n')\n"
        "if os.environ.get('SIPS_FAIL'):\n"
        "    sys.exit(1)\n"
        "open(out, 'wb').write(b'RENDERED:' + open(args[-3], 'rb').read())\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return tmp_path


def run_helper(home, *args, extra_env=None):
    env = {
        "HOME": str(home),
        "PATH": f"{home / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "SIPS_LOG": str(home / "sips.log"),
        "TMPDIR": str(home / "tmp"),
    }
    (home / "tmp").mkdir(exist_ok=True)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(HELPER), *args], env=env, capture_output=True, timeout=30,
    )


def info(home, uuid):
    result = run_helper(home, "info", uuid)
    assert result.returncode == EXIT_OK, result.stderr
    return json.loads(result.stdout)


class TestInfo:
    def test_available_original(self, home):
        record = info(home, UUID_A)
        assert record == {
            "available": True,
            "filename": "IMG_0001.HEIC",
            "size_bytes": len(b"synthetic-heic-original"),
            "kind": "photo",
            "previewable": True,
        }

    def test_size_comes_from_the_file_not_the_database(self, home):
        # The fixture records ZORIGINALFILESIZE = 0 for every asset.
        assert info(home, UUID_A)["size_bytes"] == len(b"synthetic-heic-original")

    def test_missing_original_with_derivative_is_previewable_but_unavailable(self, home):
        record = info(home, UUID_B)
        assert record["available"] is False
        assert record["size_bytes"] is None
        assert record["previewable"] is True

    def test_nothing_on_disk_is_not_previewable(self, home):
        record = info(home, UUID_C)
        assert record["available"] is False
        assert record["previewable"] is False

    def test_video(self, home):
        record = info(home, UUID_VIDEO)
        assert record["kind"] == "video"
        assert record["available"] is True
        assert record["previewable"] is True  # via its masters derivative

    def test_lowercase_uuid_is_accepted(self, home):
        assert info(home, UUID_A.lower())["filename"] == "IMG_0001.HEIC"

    def test_output_is_a_single_json_line(self, home):
        out = run_helper(home, "info", UUID_A).stdout
        assert out.count(b"\n") == 1 and out.endswith(b"\n")


class TestOriginal:
    def test_streams_the_raw_bytes(self, home):
        result = run_helper(home, "original", UUID_A)
        assert result.returncode == EXIT_OK
        assert result.stdout == b"synthetic-heic-original"

    def test_large_file_is_streamed_intact(self, home):
        big = os.urandom(300 * 1024)
        (home / "Pictures/Photos Library.photoslibrary/originals/A" / f"{UUID_A}.heic").write_bytes(big)
        assert run_helper(home, "original", UUID_A).stdout == big

    def test_video_original_is_served(self, home):
        assert run_helper(home, "original", UUID_VIDEO).stdout == b"synthetic-video-original"

    def test_absent_original_is_unavailable(self, home):
        result = run_helper(home, "original", UUID_B)
        assert result.returncode == EXIT_UNAVAILABLE
        assert result.stdout == b""


class TestPreview:
    def test_available_photo_is_rendered_with_sips_at_2048(self, home):
        result = run_helper(home, "preview", UUID_A)
        assert result.returncode == EXIT_OK
        assert result.stdout == b"RENDERED:synthetic-heic-original"
        args = (home / "sips.log").read_text().split()
        assert args[:5] == ["-Z", "2048", "-s", "format", "jpeg"]

    def test_temp_file_is_removed(self, home):
        run_helper(home, "preview", UUID_A)
        assert list((home / "tmp").iterdir()) == []

    def test_temp_file_is_removed_when_sips_fails(self, home):
        run_helper(home, "preview", UUID_A, extra_env={"SIPS_FAIL": "1"})
        assert list((home / "tmp").iterdir()) == []

    def test_missing_original_uses_largest_derivative(self, home):
        result = run_helper(home, "preview", UUID_B)
        assert result.returncode == EXIT_OK
        assert result.stdout == b"medium-B"  # _1_102_o beats _1_105_c
        assert not (home / "sips.log").exists()

    def test_derivative_order_medium_small_masters(self, home):
        library = home / "Pictures/Photos Library.photoslibrary"
        (library / f"resources/derivatives/B/{UUID_B}_1_102_o.jpeg").unlink()
        assert run_helper(home, "preview", UUID_B).stdout == b"small-B"
        (library / f"resources/derivatives/B/{UUID_B}_1_105_c.jpeg").unlink()
        masters = library / f"resources/derivatives/masters/B/{UUID_B}_4_5005_c.jpeg"
        masters.parent.mkdir(parents=True)
        masters.write_bytes(b"masters-B")
        assert run_helper(home, "preview", UUID_B).stdout == b"masters-B"

    def test_sips_failure_falls_back_to_derivative(self, home):
        result = run_helper(home, "preview", UUID_A, extra_env={"SIPS_FAIL": "1"})
        assert result.returncode == EXIT_OK
        assert result.stdout == b"medium-A"

    def test_sips_failure_without_derivative_is_unavailable(self, home):
        library = home / "Pictures/Photos Library.photoslibrary"
        for template in (MASTERS, SMALL, MEDIUM):
            (library / template.format(first="A", uuid=UUID_A)).unlink()
        result = run_helper(home, "preview", UUID_A, extra_env={"SIPS_FAIL": "1"})
        assert result.returncode == EXIT_UNAVAILABLE

    def test_video_uses_derivative_not_sips(self, home):
        result = run_helper(home, "preview", UUID_VIDEO)
        assert result.returncode == EXIT_OK
        assert result.stdout == b"masters-video"
        assert not (home / "sips.log").exists()

    def test_video_without_derivative_is_unavailable(self, home):
        library = home / "Pictures/Photos Library.photoslibrary"
        (library / MASTERS.format(first="D", uuid=UUID_VIDEO)).unlink()
        assert run_helper(home, "preview", UUID_VIDEO).returncode == EXIT_UNAVAILABLE

    def test_nothing_on_disk_is_unavailable(self, home):
        result = run_helper(home, "preview", UUID_C)
        assert result.returncode == EXIT_UNAVAILABLE
        assert result.stdout == b""


class TestExitCodes:
    @pytest.mark.parametrize("command", ["info", "original", "preview"])
    def test_unknown_asset_is_not_found(self, home, command):
        assert run_helper(home, command, UUID_UNKNOWN).returncode == EXIT_NOT_FOUND

    @pytest.mark.parametrize("command", ["info", "original", "preview"])
    def test_no_library_is_not_found(self, tmp_path, command):
        (tmp_path / "bin").mkdir()
        assert run_helper(tmp_path, command, UUID_A).returncode == EXIT_NOT_FOUND

    @pytest.mark.parametrize("args", [
        [],
        ["info"],
        ["bogus", UUID_A],
        ["info", "not-a-uuid"],
        ["info", "../../etc/passwd"],
        ["info", UUID_A + "x"],
        ["info", UUID_A, "extra"],
        ["info", "'; DROP TABLE ZASSET; --"],
    ])
    def test_usage_errors(self, home, args):
        result = run_helper(home, *args)
        assert result.returncode == EXIT_USAGE
        assert result.stdout == b""

    def test_unreadable_database_is_an_internal_error(self, home):
        db = home / "Pictures/Photos Library.photoslibrary/database/Photos.sqlite"
        db.write_bytes(b"this is not a sqlite database" * 100)
        assert run_helper(home, "info", UUID_A).returncode == EXIT_INTERNAL


class TestPathHandling:
    def test_original_outside_the_originals_tree_is_not_served(self, home):
        library = home / "Pictures/Photos Library.photoslibrary"
        secret = home / "secret.txt"
        secret.write_bytes(b"outside the library")
        conn = sqlite3.connect(str(library / "database/Photos.sqlite"))
        conn.execute(
            "UPDATE ZASSET SET ZDIRECTORY = '../../..', ZFILENAME = 'secret.txt' WHERE ZUUID = ?",
            (UUID_A,),
        )
        conn.commit()
        conn.close()
        assert info(home, UUID_A)["available"] is False
        result = run_helper(home, "original", UUID_A)
        assert result.returncode == EXIT_UNAVAILABLE
        assert b"outside the library" not in result.stdout

    def test_uuid_is_passed_as_a_bound_parameter(self, home):
        # A quote in the UUID never reaches SQL: it fails validation first.
        result = run_helper(home, "info", "A" * 35 + "'")
        assert result.returncode == EXIT_USAGE

    def test_library_on_a_mounted_volume_is_not_required(self, home):
        # The default location is found without any /Volumes entry.
        assert info(home, UUID_A)["available"] is True


class TestReadOnly:
    def test_database_is_opened_read_only(self, home):
        sys.path.insert(0, str(HELPER.parent))
        try:
            import photos_origin_helper
        finally:
            sys.path.pop(0)
        library = home / "Pictures/Photos Library.photoslibrary"
        conn = photos_origin_helper._open_readonly(library)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("DELETE FROM ZASSET")
        finally:
            conn.close()

    def test_library_is_unchanged_by_every_command(self, home):
        library = home / "Pictures/Photos Library.photoslibrary"

        def snapshot():
            return {
                str(p): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
                for p in sorted(library.rglob("*")) if p.is_file()
            }

        before = snapshot()
        for command in ("info", "original", "preview"):
            for uuid in (UUID_A, UUID_B, UUID_VIDEO):
                run_helper(home, command, uuid)
        assert snapshot() == before


class TestPython39Compatibility:
    def test_source_parses_as_python_39(self):
        ast.parse(HELPER.read_text(), feature_version=(3, 9))

    def test_no_runtime_evaluated_pep604_unions_or_match_statements(self):
        tree = ast.parse(HELPER.read_text(), feature_version=(3, 9))
        assert not [n for n in ast.walk(tree) if type(n).__name__ == "Match"]
        source = HELPER.read_text()
        assert "from __future__ import annotations" not in source
        annotations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.arg) and node.annotation:
                annotations.append(node.annotation)
            elif isinstance(node, ast.FunctionDef) and node.returns:
                annotations.append(node.returns)
            elif isinstance(node, ast.AnnAssign):
                annotations.append(node.annotation)
        for annotation in annotations:
            for sub in ast.walk(annotation):
                assert not (isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr)), (
                    ast.dump(annotation)
                )

    def test_imports_only_the_standard_library(self):
        tree = ast.parse(HELPER.read_text())
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module.split(".")[0])
        assert modules <= set(sys.stdlib_module_names)

    def test_no_shell_interpolation(self):
        source = HELPER.read_text()
        assert "shell=True" not in source and "os.system" not in source
        assert re.search(r"execute\(\s*f[\"']", source) is None
