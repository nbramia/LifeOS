#!/usr/bin/env python3
"""Read-only access to a local Apple Photos library, for the LifeOS server.

Runs on the Mac that holds the Photos library, invoked over SSH by the
LifeOS API (``api/services/photos_origin.py``):

    /usr/bin/python3 scripts/photos_origin_helper.py info <uuid>
    /usr/bin/python3 scripts/photos_origin_helper.py original <uuid>
    /usr/bin/python3 scripts/photos_origin_helper.py preview <uuid>

Standard library only and Python 3.9 compatible: the system interpreter on
macOS is 3.9, and this file is also imported by ``apple_data_export.py`` for
its library discovery and derivative-preference constants.

Subcommands:
    info      One JSON line: {available, filename, size_bytes, kind, previewable}.
              ``available`` means the original file exists on disk.
    original  The raw original bytes on stdout.
    preview   A browser-renderable JPEG on stdout: the original downscaled with
              ``sips`` when it is on disk, otherwise the best existing derivative.

Exit codes:
    0  success
    1  internal error
    2  usage error or malformed UUID
    3  the asset is not in the library
    4  the asset is known but nothing servable is on disk

The library is opened read-only and never modified.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_UNAVAILABLE = 4

UUID_RE = re.compile(r"^[0-9A-Fa-f-]{36}$")

# Largest edge, in pixels, of a generated preview.
PREVIEW_MAX_DIMENSION = 2048
SIPS_TIMEOUT_SECONDS = 60
CHUNK_BYTES = 64 * 1024

# Small derivatives staged for the server's thumbnail route, best first.
# Paths are relative to <library>/resources/derivatives/.
THUMBNAIL_DERIVATIVES = (
    "masters/{first}/{uuid}_4_5005_c.jpeg",
    "{first}/{uuid}_1_105_c.jpeg",
    "{first}/{uuid}_1_102_o.jpeg",
)

# Derivatives usable as a preview when the original is not on disk, largest
# first.
PREVIEW_DERIVATIVES = (
    "{first}/{uuid}_1_102_o.jpeg",
    "{first}/{uuid}_1_105_c.jpeg",
    "masters/{first}/{uuid}_4_5005_c.jpeg",
)

KIND_PHOTO = 0
KIND_VIDEO = 1


def library_candidates() -> List[Path]:
    """Photos library locations to try, in order: the default location, then
    any library on a mounted volume."""
    candidates = [Path.home() / "Pictures" / "Photos Library.photoslibrary"]
    volumes = Path("/Volumes")
    try:
        candidates.extend(sorted(volumes.glob("*/Photos Library.photoslibrary")))
    except OSError:
        pass
    return candidates


def find_library() -> Optional[Path]:
    """The first candidate library whose Photos.sqlite exists, or None."""
    for candidate in library_candidates():
        try:
            if (candidate / "database" / "Photos.sqlite").exists():
                return candidate
        except OSError:
            continue
    return None


def derivative_paths(library: Path, asset_uuid: str, patterns) -> List[Path]:
    """Existing derivative files for an asset, in the order given by patterns."""
    base = library / "resources" / "derivatives"
    first = asset_uuid[0].upper()
    found = []
    for pattern in patterns:
        path = base / pattern.format(first=first, uuid=asset_uuid)
        if path.is_file():
            found.append(path)
    return found


def _open_readonly(library: Path) -> sqlite3.Connection:
    db_path = library / "database" / "Photos.sqlite"
    conn = sqlite3.connect("file:%s?mode=ro" % quote(str(db_path)), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _original_path(library: Path, directory: Optional[str], filename: Optional[str]) -> Optional[Path]:
    """The on-disk original, or None when absent or outside the originals tree."""
    if not directory or not filename:
        return None
    originals = (library / "originals").resolve()
    try:
        candidate = (originals / directory / filename).resolve()
    except OSError:
        return None
    if originals not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def lookup_asset(library: Path, asset_uuid: str) -> Optional[dict]:
    """Describe an asset, or None when the library has no such asset.

    The reported size is the file on disk; the database's recorded size can
    be zero or stale.
    """
    conn = _open_readonly(library)
    try:
        row = conn.execute(
            """
            SELECT a.ZDIRECTORY AS directory, a.ZFILENAME AS filename,
                   a.ZKIND AS kind, aa.ZORIGINALFILENAME AS original_filename
            FROM ZASSET a
            LEFT JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK
            WHERE a.ZUUID = ?
            """,
            (asset_uuid,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None

    path = _original_path(library, row["directory"], row["filename"])
    kind = {KIND_PHOTO: "photo", KIND_VIDEO: "video"}.get(row["kind"], "unknown")
    derivatives = derivative_paths(library, asset_uuid, PREVIEW_DERIVATIVES)
    return {
        "path": path,
        "filename": row["original_filename"] or row["filename"],
        "size_bytes": path.stat().st_size if path else None,
        "kind": kind,
        "derivatives": derivatives,
        "previewable": bool(derivatives) or (path is not None and kind == "photo"),
    }


def _copy_to_stdout(path: Path) -> None:
    out = sys.stdout.buffer
    with open(str(path), "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_BYTES)
            if not chunk:
                break
            out.write(chunk)
    out.flush()


def _render_with_sips(source: Path) -> Optional[Path]:
    """Downscale an original to a temporary JPEG; the caller removes it.

    Returns None when sips is missing, fails, or times out.
    """
    fd, tmp_name = tempfile.mkstemp(suffix=".jpg", prefix="lifeos-preview-")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        subprocess.run(
            [
                "sips", "-Z", str(PREVIEW_MAX_DIMENSION),
                "-s", "format", "jpeg", str(source), "--out", str(tmp),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=SIPS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        tmp.unlink()
        return None
    if tmp.stat().st_size == 0:
        tmp.unlink()
        return None
    return tmp


def cmd_info(library: Path, asset: dict) -> int:
    print(json.dumps({
        "available": asset["path"] is not None,
        "filename": asset["filename"],
        "size_bytes": asset["size_bytes"],
        "kind": asset["kind"],
        "previewable": asset["previewable"],
    }))
    return EXIT_OK


def cmd_original(library: Path, asset: dict) -> int:
    if asset["path"] is None:
        return EXIT_UNAVAILABLE
    _copy_to_stdout(asset["path"])
    return EXIT_OK


def cmd_preview(library: Path, asset: dict) -> int:
    if asset["path"] is not None and asset["kind"] == "photo":
        rendered = _render_with_sips(asset["path"])
        if rendered is not None:
            try:
                _copy_to_stdout(rendered)
            finally:
                rendered.unlink()
            return EXIT_OK
    if asset["derivatives"]:
        _copy_to_stdout(asset["derivatives"][0])
        return EXIT_OK
    return EXIT_UNAVAILABLE


COMMANDS = {"info": cmd_info, "original": cmd_original, "preview": cmd_preview}


def main(argv: List[str]) -> int:
    if len(argv) != 3 or argv[1] not in COMMANDS or not UUID_RE.match(argv[2]):
        sys.stderr.write("usage: photos_origin_helper.py {info|original|preview} <uuid>\n")
        return EXIT_USAGE
    asset_uuid = argv[2].upper()

    library = find_library()
    if library is None:
        return EXIT_NOT_FOUND
    try:
        asset = lookup_asset(library, asset_uuid)
        if asset is None:
            return EXIT_NOT_FOUND
        return COMMANDS[argv[1]](library, asset)
    except BrokenPipeError:
        return EXIT_INTERNAL
    except (OSError, sqlite3.Error) as exc:
        sys.stderr.write("photos_origin_helper: %s\n" % type(exc).__name__)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main(sys.argv))
