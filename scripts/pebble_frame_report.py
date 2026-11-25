#!/usr/bin/env python3
"""
Count Pebble ready-result frames by signature state, read-only.

This is the check before turning on LIFEOS_PEBBLE_REQUIRE_FRAME_SIGNATURE.
For every ready result frame in the capture directory it reports whether the
MAC line verifies against LIFEOS_PEBBLE_FRAME_KEY_FILE and what the receipt
ledger holds for it. Frames counted under `would_refuse` are the ones the
watcher would refuse once signatures are required: they must be zero, or
known not to come from Pebble, before the switch.

It prints counts only, never a transcript, path, or capture id. It opens the
ledger read-only and writes nothing.

Usage:
    ~/.venvs/lifeos/bin/python scripts/pebble_frame_report.py [--capture-dir DIR] [--ledger DB]
"""
import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.pebble_capture import (  # noqa: E402
    FRAME_SIGNED,
    load_frame_key,
    parse_frames,
    payload_digest,
    ready_result,
)
from api.services.pebble_capture_watcher import _eligible  # noqa: E402
from config.settings import settings  # noqa: E402


def _ledger_rows(path: Path) -> tuple[dict, set]:
    if not path.exists():
        return {}, set()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        plans = {
            (source_id, capture_id): (revision, digest)
            for source_id, capture_id, revision, digest in db.execute(
                "SELECT source_id, capture_id, revision, payload_digest FROM pebble_captures"
            )
        }
        complete = {
            (source_id, capture_id)
            for source_id, capture_id in db.execute(
                "SELECT source_id, capture_id FROM pebble_effects "
                "WHERE action_index=-1 AND state='applied'"
            )
        }
    return plans, complete


def report(capture_dir: Path, ledger_path: Path, key: bytes | None) -> dict:
    plans, complete = _ledger_rows(ledger_path)
    counts: Counter = Counter()
    for path in sorted(capture_dir.iterdir()):
        if not _eligible(path, capture_dir):
            continue
        for frame in parse_frames(path.read_bytes(), key):
            try:
                identity, revision, _ = ready_result(frame.payload)
            except Exception:
                continue
            ident = (identity.source_id, identity.capture_id)
            admitted = plans.get(ident) == (revision, payload_digest(frame.payload))
            state = "complete" if ident in complete else "planned" if admitted else "new"
            counts[f"{frame.signature}.{state}"] += 1
            if frame.signature != FRAME_SIGNED and not admitted:
                counts["would_refuse"] += 1
    return {"key_loaded": key is not None, "would_refuse": 0, **dict(sorted(counts.items()))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--capture-dir", type=Path,
                        default=settings.vault_path / settings.pebble_capture_dir)
    parser.add_argument("--ledger", type=Path, default=Path("data") / "pebble_capture.db")
    args = parser.parse_args()
    key = load_frame_key(settings.pebble_frame_key_file)
    print(json.dumps(report(args.capture_dir.resolve(), args.ledger, key), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
