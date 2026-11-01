"""Synthetic Apple Photos library builder shared by the Photos tests.

Builds the small slice of a ``.photoslibrary`` bundle that
``scripts/photos_origin_helper.py`` and ``scripts/apple_data_export.py`` read:
``database/Photos.sqlite`` (ZASSET, ZADDITIONALASSETATTRIBUTES, ZPERSON,
ZDETECTEDFACE), ``originals/<dir>/<file>``, and the ``resources/derivatives``
tree. All names, UUIDs, and bytes are obviously synthetic.
"""
import sqlite3
from pathlib import Path
from typing import Optional

UUID_A = "AAAAAAAA-0000-4000-8000-000000000001"
UUID_B = "BBBBBBBB-0000-4000-8000-000000000002"
UUID_C = "CCCCCCCC-0000-4000-8000-000000000003"
UUID_VIDEO = "DDDDDDDD-0000-4000-8000-000000000004"
UUID_UNKNOWN = "EEEEEEEE-0000-4000-8000-0000000000FF"

FAKE_JPEG = b"\xff\xd8\xff\xe0synthetic-jpeg-bytes\xff\xd9"

# Library-relative derivative path templates.
MASTERS = "resources/derivatives/masters/{first}/{uuid}_4_5005_c.jpeg"
SMALL = "resources/derivatives/{first}/{uuid}_1_105_c.jpeg"
MEDIUM = "resources/derivatives/{first}/{uuid}_1_102_o.jpeg"


def make_library(
    root: Path,
    assets: list,
    people: Optional[list] = None,
    faces: Optional[list] = None,
) -> Path:
    """Create ``<root>/Pictures/Photos Library.photoslibrary`` and return it.

    Each asset dict: uuid, kind (0 photo / 1 video), directory, filename,
    original_filename, original (bytes, or None when the file is not on
    disk), derivatives (dict of the templates above to bytes).
    people: (pk, full_name, face_count, person_uri). faces: (asset_pk, person_pk).
    """
    library = root / "Pictures" / "Photos Library.photoslibrary"
    (library / "database").mkdir(parents=True)
    conn = sqlite3.connect(str(library / "database" / "Photos.sqlite"))
    conn.executescript(
        """
        CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, ZDIRECTORY TEXT,
            ZFILENAME TEXT, ZKIND INTEGER, ZDATECREATED REAL, ZLATITUDE REAL,
            ZLONGITUDE REAL);
        CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY,
            ZASSET INTEGER, ZORIGINALFILENAME TEXT, ZORIGINALFILESIZE INTEGER);
        CREATE TABLE ZPERSON (Z_PK INTEGER PRIMARY KEY, ZFULLNAME TEXT,
            ZDISPLAYNAME TEXT, ZFACECOUNT INTEGER, ZPERSONURI TEXT);
        CREATE TABLE ZDETECTEDFACE (Z_PK INTEGER PRIMARY KEY,
            ZASSETFORFACE INTEGER, ZPERSONFORFACE INTEGER);
        """
    )
    for pk, asset in enumerate(assets, start=1):
        conn.execute(
            "INSERT INTO ZASSET VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
            (pk, asset["uuid"], asset["directory"], asset["filename"],
             asset.get("kind", 0), 700000000.0 + pk),
        )
        # The recorded size is deliberately wrong: the helper must trust the
        # file on disk.
        conn.execute(
            "INSERT INTO ZADDITIONALASSETATTRIBUTES VALUES (?, ?, ?, 0)",
            (pk, pk, asset.get("original_filename")),
        )
        if asset.get("original") is not None:
            path = library / "originals" / asset["directory"] / asset["filename"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(asset["original"])
        for template, data in asset.get("derivatives", {}).items():
            path = library / template.format(first=asset["uuid"][0], uuid=asset["uuid"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    for person in people or []:
        conn.execute(
            "INSERT INTO ZPERSON VALUES (?, ?, ?, ?, ?)",
            (person[0], person[1], person[1], person[2], person[3]),
        )
    for n, (asset_pk, person_pk) in enumerate(faces or [], start=1):
        conn.execute("INSERT INTO ZDETECTEDFACE VALUES (?, ?, ?)", (n, asset_pk, person_pk))
    conn.commit()
    conn.close()
    return library


def standard_assets() -> list:
    """Photo with original + derivatives, photo with only a derivative,
    photo with nothing on disk, and a video with a derivative."""
    return [
        {
            "uuid": UUID_A, "kind": 0, "directory": "A", "filename": f"{UUID_A}.heic",
            "original_filename": "IMG_0001.HEIC", "original": b"synthetic-heic-original",
            "derivatives": {MASTERS: b"masters-A", SMALL: b"small-A", MEDIUM: b"medium-A"},
        },
        {
            "uuid": UUID_B, "kind": 0, "directory": "B", "filename": f"{UUID_B}.jpeg",
            "original_filename": "IMG_0002.JPG", "original": None,
            "derivatives": {SMALL: b"small-B", MEDIUM: b"medium-B"},
        },
        {
            "uuid": UUID_C, "kind": 0, "directory": "C", "filename": f"{UUID_C}.jpeg",
            "original_filename": "IMG_0003.JPG", "original": None, "derivatives": {},
        },
        {
            "uuid": UUID_VIDEO, "kind": 1, "directory": "D", "filename": f"{UUID_VIDEO}.mov",
            "original_filename": "MOV_0004.MOV", "original": b"synthetic-video-original",
            "derivatives": {MASTERS: b"masters-video"},
        },
    ]
