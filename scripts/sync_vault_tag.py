#!/usr/bin/env python3
"""
Tag vault notes (nightly, incremental).

Walks every ``.md`` note under the vault, skips notes whose content hash and
the taxonomy's vocabulary version match the stored tag row, tags the rest with
``VaultTagger``, and prunes rows for notes absent from the vault. Not an
embedding source: no GPU and no local-LLM pause.

Runs only when ``LIFEOS_JEV_VAULT_TAGGING`` is ``shadow`` or ``on`` and a
TypeSafe API key is configured; otherwise it reports ``SYNC_SKIPPED`` and
changes nothing. Per-note failures are counted, never fatal.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

MAX_WORKERS = 8
EXCLUDED_DIRS = (".obsidian", ".trash", "Wiki/Vault Map")
LOW_CONFIDENCE_BELOW = 0.6

STAT_KEYS = ("tagged", "skipped_unchanged", "skipped_not_allowlisted", "code_only", "restricted", "low_confidence", "errors")


def _excluded(rel: Path) -> bool:
    rel_s = rel.as_posix()
    return any(rel_s == d or rel_s.startswith(d + "/") for d in EXCLUDED_DIRS)


def list_vault_notes(vault: Path) -> list[Path]:
    """Every ``.md`` file under the vault except the excluded folders."""
    return sorted(p for p in vault.rglob("*.md") if p.is_file() and not _excluded(p.relative_to(vault)))


def _configured() -> bool:
    from api.services.vault_tagger import _mode
    from config.settings import settings

    return _mode() in ("shadow", "on") and settings.jev_configured


def sync_vault_tag(dry_run: bool = True, store=None, tagger=None, vault_path=None) -> dict:
    """Tag changed notes; returns the stats dict (see ``STAT_KEYS`` plus
    ``vocab_version``, and ``status``/``would_tag`` when not a plain run)."""
    from api.services.vault_tag_store import VaultTagStore
    from api.services.vault_tagger import VaultTagger, is_allowlisted
    from api.services.vault_taxonomy import load_taxonomy
    from config.settings import settings

    stats = {k: 0 for k in STAT_KEYS}
    vault = Path(vault_path or settings.vault_path)
    if tagger is None and not _configured():
        stats.update(status="skipped", vocab_version="")
        return stats
    if not vault.is_dir():
        logger.error("Vault path not found")
        stats.update(status="error", vocab_version="", errors=1)
        return stats

    store = store or VaultTagStore()
    tagger = tagger or VaultTagger(store=store, vault_root=vault)
    vocab = load_taxonomy().vocab_version
    stats["vocab_version"] = vocab

    notes = list_vault_notes(vault)
    rels = {p: p.relative_to(vault).as_posix() for p in notes}

    def work(path: Path) -> tuple[str, object]:
        rel = rels[path]
        try:
            sha = hashlib.sha256(path.read_text(encoding="utf-8", errors="replace").encode("utf-8")).hexdigest()
            if not store.needs_tagging(rel, sha, vocab):
                # A code-only restricted row may have become sendable through a setting change.
                row = store.get(rel)
                if not (row and row.backend == "code" and row.sensitivity == "restricted"
                        and tagger.would_send(path)):
                    return "unchanged", None
            if dry_run:
                return "would_tag", None
            record = tagger.tag_file(path)
            # tag_file never raises; a fallback record carries a stale hash or vocab.
            if record.content_sha256 != sha or record.vocab_version != vocab:
                return "error", None
            store.upsert(record)
            return "tagged", (record, is_allowlisted(rel))
        except Exception as exc:  # noqa: BLE001 - one bad note must not fail the run
            logger.warning("Vault tagging failed for a note: %s", type(exc).__name__)
            return "error", None

    would_tag = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for outcome, payload in pool.map(work, notes):
            if outcome == "unchanged":
                stats["skipped_unchanged"] += 1
            elif outcome == "would_tag":
                would_tag += 1
            elif outcome == "error":
                stats["errors"] += 1
            else:
                record, allowed = payload
                stats["tagged"] += 1
                if not allowed:
                    stats["skipped_not_allowlisted"] += 1
                if record.sensitivity == "restricted":
                    stats["restricted"] += 1
                if record.backend == "code":
                    stats["code_only"] += 1
                elif record.topic_conf is not None and record.topic_conf < LOW_CONFIDENCE_BELOW:
                    stats["low_confidence"] += 1

    if dry_run:
        logger.info("DRY RUN - would tag %d of %d notes", would_tag, len(notes))
        stats.update(status="dry_run", would_tag=would_tag)
        return stats

    removed = store.delete_missing(rels.values())
    logger.info("Vault tagging: %d tagged, %d unchanged, %d errors, %d pruned",
                stats["tagged"], stats["skipped_unchanged"], stats["errors"], removed)
    stats["status"] = "success"
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Tag vault notes')
    parser.add_argument('--execute', action='store_true', help='Tag changed notes')
    parser.add_argument('--dry-run', action='store_true', help='Count notes that would be tagged; send nothing')
    args = parser.parse_args(argv)

    result = sync_vault_tag(dry_run=args.dry_run or not args.execute)

    if result["status"] == "skipped":
        print("SYNC_SKIPPED: vault tagging not configured — set LIFEOS_JEV_VAULT_TAGGING "
              "to shadow or on and configure a TypeSafe API key", flush=True)

    from api.services.sync_health import emit_sync_stats
    # Only int values are read by the orchestrator; vocab_version is informational.
    emit_sync_stats({**{k: result[k] for k in STAT_KEYS}, "vocab_version": result["vocab_version"]})
    return 0


if __name__ == '__main__':
    sys.exit(main())
