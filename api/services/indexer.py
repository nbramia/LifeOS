"""
Indexer service for LifeOS.

Indexes Obsidian vault content to ChromaDB and BM25.
Supports incremental indexing based on file modification times.
"""
import gc
import os
import json
import logging
import time
from pathlib import Path
from datetime import datetime, timezone

from api.services.chunker import chunk_document, extract_frontmatter, add_context_to_chunks, normalize_tags, tag_phrase_key
from api.services.vectorstore import VectorStore, is_people_tag
from api.services.bm25_index import BM25Index
from api.services.people import people_in_note
from api.services.vault_listing import in_underscore_dir, no_people_reason

# V2 People System integration
try:
    from api.services.entity_resolver import EntityResolver, get_entity_resolver  # noqa: F401
    from api.services.interaction_store import (
        InteractionStore,  # noqa: F401
        get_interaction_store,
        create_vault_interaction,
        UNDATED_SENTINEL,
    )
    from api.services.source_entity import (
        LINK_STATUS_AUTO,
        LINK_STATUS_CONFIRMED,
        get_source_entity_store,
        create_vault_source_entity,
        create_granola_source_entity,
    )
    from api.services.mention_resolver import (
        MentionResolver,
        build_note_context,
        is_ambiguous_mention,
        load_link_overrides,
    )
    HAS_V2_PEOPLE = True
except ImportError:
    HAS_V2_PEOPLE = False

logger = logging.getLogger(__name__)


def _mtime_trust_cutoff() -> datetime | None:
    """Parse `LIFEOS_VAULT_MTIME_TRUSTED_AFTER` (YYYY-MM-DD) into a UTC datetime.

    Returns None if the env var is unset or unparseable. When None, undated
    notes get UNDATED_SENTINEL — never the filesystem mtime — to avoid
    polluting the timeline with bulk-migration / restore-from-backup mtimes
    on installations that don't opt in.
    """
    raw = os.environ.get("LIFEOS_VAULT_MTIME_TRUSTED_AFTER", "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning(
            "LIFEOS_VAULT_MTIME_TRUSTED_AFTER=%r is not a valid YYYY-MM-DD date; "
            "ignoring (undated notes will get UNDATED_SENTINEL)",
            raw,
        )
        return None


_MTIME_TRUST_CUTOFF = _mtime_trust_cutoff()


def _resolve_undated_note_date(path) -> datetime:
    """Return the best-effort date for an undated note's interactions.

    Uses the file's mtime when it is strictly later than
    `LIFEOS_VAULT_MTIME_TRUSTED_AFTER`; otherwise falls back to
    UNDATED_SENTINEL. See the env-var docstring on `_mtime_trust_cutoff`
    for the rationale (bulk migrations cluster mtimes).
    """
    if _MTIME_TRUST_CUTOFF is None:
        return UNDATED_SENTINEL
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except Exception:
        return UNDATED_SENTINEL
    if mtime > _MTIME_TRUST_CUTOFF:
        return mtime
    return UNDATED_SENTINEL


# Reserved key in the index state: file path -> tag tuple used at index time.
TAG_STATE_KEY = "__tag_phrase_keys__"

# What a tag lookup gives when the store failed, as opposed to having no row.
_TAG_LOOKUP_FAILED = object()

# How much of a note to read when ordering files for a time budget: enough for
# frontmatter. A note whose frontmatter runs longer sorts as ordinary.
BUDGET_ORDER_HEAD_CHARS = 8192

# How long one built MentionResolver stands in for the people store.
MENTION_RESOLVER_MAX_AGE_SECONDS = 600


class IndexerService:
    """
    Main indexer service.

    Handles indexing of Obsidian vault files to ChromaDB.
    Supports incremental indexing based on file modification times.
    """

    # State file for tracking indexed files
    INDEX_STATE_FILE = "data/vault_index_state.json"

    _mention_resolver: "MentionResolver | None" = None
    _mention_resolver_built_at = 0.0

    # Budgets are measured on this clock.
    _clock = staticmethod(time.monotonic)
    # What the latest index_all did beyond its return value.
    last_run: dict = {"files_pruned": 0, "files_remaining": 0, "stopped_early": False}

    def __init__(
        self,
        vault_path: str,
        db_path: str = "./data/chromadb",
        interaction_store=None,
        source_entity_store=None,
        entity_resolver=None,
        tag_store=None,
        connect_stores: bool = True,
    ):
        """
        Initialize indexer.

        Args:
            vault_path: Path to Obsidian vault
            db_path: Path to ChromaDB database
            interaction_store: Optional InteractionStore (uses singleton if None)
            source_entity_store: Optional SourceEntityStore (uses singleton if None)
            entity_resolver: Optional EntityResolver (uses singleton if None)
            tag_store: Optional VaultTagStore whose tags feed chunk context
                (opened lazily from the default location if None)
            connect_stores: If False, open neither the vector store nor BM25.
                Such an instance can only answer ``scan_changes``.
        """
        self.vault_path = Path(vault_path)
        self.db_path = Path(db_path)

        # Store injected dependencies (or None to use singletons)
        self._interaction_store = interaction_store
        self._source_entity_store = source_entity_store
        self._entity_resolver = entity_resolver
        self._tag_store = tag_store
        self._tag_store_failed = False
        self._tag_lookup_failed = False

        if connect_stores:
            # Initialize vector store
            self.vector_store = VectorStore()

            # Initialize BM25 keyword index
            self.bm25_index = BM25Index()

    def _tag_for(self, path: Path):
        """The file's TagRecord, or None when the store or the row is missing."""
        tag = self._lookup_tag(path)
        return None if tag is _TAG_LOOKUP_FAILED else tag

    def _lookup_tag(self, path: Path):
        """The file's TagRecord, None if it has no row, or ``_TAG_LOOKUP_FAILED``.

        Rows are keyed by vault-relative POSIX path, the same key
        ``scripts/sync_vault_tag.py`` writes.
        """
        if self._tag_store is None:
            if self._tag_store_failed:
                return _TAG_LOOKUP_FAILED
            try:
                from api.services.vault_tag_store import VaultTagStore
                self._tag_store = VaultTagStore()
            except Exception as e:
                logger.warning(f"Tag store unavailable, indexing without tag phrases: {e}")
                self._tag_store_failed = True
                return _TAG_LOOKUP_FAILED
        try:
            key = Path(path).relative_to(self.vault_path).as_posix()
            return self._tag_store.get(key)
        except Exception as e:
            if not self._tag_lookup_failed:
                logger.warning(f"Tag lookup failed, leaving tag state as recorded: {e}")
                self._tag_lookup_failed = True
            return _TAG_LOOKUP_FAILED

    def _load_index_state(self) -> dict:
        """Load the index state (file paths -> last indexed mtime)."""
        state_path = Path(self.INDEX_STATE_FILE)
        if state_path.exists():
            try:
                return json.loads(state_path.read_text())
            except Exception as e:
                logger.warning(f"Failed to load index state: {e}")
        return {}

    def _save_index_state(self, state: dict) -> None:
        """Save the index state."""
        state_path = Path(self.INDEX_STATE_FILE)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        # Replaced atomically: a process killed mid-write must not leave a torn
        # file, which would load as empty state and re-index the whole vault.
        tmp_path = state_path.with_name(f"{state_path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(state, indent=2))
        os.replace(tmp_path, state_path)

    def _ordered_for_budget(self, files: list[tuple[str, float]]) -> list[tuple[str, float]]:
        """Ordinary notes before bulk content, each newest first, so a budget
        spent on a bulk load has already covered what the owner wrote."""
        def is_bulk(file_path: str) -> bool:
            try:
                with open(file_path, encoding="utf-8", errors="replace") as f:
                    head = f.read(BUDGET_ORDER_HEAD_CHARS)
            except OSError:
                return False
            return self._no_people_reason(Path(file_path), extract_frontmatter(head)[0]) is not None

        return sorted(files, key=lambda item: (is_bulk(item[0]), -item[1]))

    def scan_changes(self, compare_tags: bool = True) -> dict:
        """Report what an incremental ``index_all`` would do, changing nothing.

        Needs no vector store or BM25, so a caller can learn that there is no
        work without loading anything heavy.

        Args:
            compare_tags: If False, ignore tag phrases and compare modification
                times only. For a caller that has no tag store to read: treating
                every note as untagged would report each tagged one as changed.

        Returns:
            Dict with ``to_index`` (newest modification time first), ``deleted``
            and ``file_count``.
        """
        index_state = self._load_index_state()
        files_to_index, deleted_files, _tag_key, file_count = self._find_changes(
            index_state, force=False, compare_tags=compare_tags
        )
        return {
            "to_index": [path for path, _ in sorted(files_to_index, key=lambda item: item[1], reverse=True)],
            "deleted": sorted(deleted_files),
            "file_count": file_count,
        }

    def _find_changes(self, index_state: dict, force: bool, compare_tags: bool = True) -> tuple[list, set, dict, int]:
        """Compare the vault with ``index_state``.

        Returns:
            ``(files_to_index, deleted_files, current_tag_key, file_count)``,
            with ``files_to_index`` as ``(path, mtime)`` pairs in vault walk order.
        """
        # Get all current markdown files with their mtimes. A file under a
        # _-prefixed folder isn't a note, so an indexed one drops out through
        # the deleted-file path below.
        all_md_files = [
            f for f in self.vault_path.rglob("*.md")
            if not in_underscore_dir(f.relative_to(self.vault_path))
        ]
        current_files = {str(f): f.stat().st_mtime for f in all_md_files}

        # Tag tuples used at index time live under a reserved key beside the
        # per-file mtimes. A file whose current tuple differs is re-indexed
        # even when its bytes did not change; no row and no phrase is None on
        # both sides, so untagged files are never re-indexed for it.
        tag_keys: dict = index_state.get(TAG_STATE_KEY) or {}
        current_tag_key: dict[str, list | None] = {}

        # Determine which files need indexing
        files_to_index = []
        for file_path, mtime in current_files.items():
            prev_mtime = index_state.get(file_path)
            tags_changed = False
            if compare_tags:
                tag = self._lookup_tag(Path(file_path))
                key = None if tag is _TAG_LOOKUP_FAILED else tag_phrase_key(tag)
                current_tag_key[file_path] = list(key) if key else None
                # A failed lookup says nothing about the tags, so it can't make a
                # note look changed. A note indexed for another reason meanwhile
                # has no phrase in its chunks and records no key, so the next
                # good lookup indexes it once more to restore the phrase.
                tags_changed = tag is not _TAG_LOOKUP_FAILED and current_tag_key[file_path] != tag_keys.get(file_path)
            if force:
                # In force mode, reindex if not yet indexed in this run
                # (allows resuming a force reindex after crash)
                if prev_mtime is None or prev_mtime < mtime:
                    files_to_index.append((file_path, mtime))
            else:
                # Normal incremental: only if file or its tag phrase changed
                if prev_mtime is None or mtime > prev_mtime or tags_changed:
                    files_to_index.append((file_path, mtime))

        # Determine deleted files (in old state but not in current files)
        deleted_files = set(index_state.keys()) - set(current_files.keys()) - {TAG_STATE_KEY}

        return files_to_index, deleted_files, current_tag_key, len(all_md_files)

    def index_all(
        self,
        force: bool = False,
        skip_summaries: bool = False,
        time_budget_seconds: float | None = None,
        max_files: int | None = None,
        newest_first: bool = False,
        light_pass: bool = False,
        max_deletions: int | None = None,
    ) -> int:
        """
        Index markdown files in the vault.

        Uses incremental indexing by default - only indexes files that have
        changed since the last index run. Use force=True to reindex everything.

        Progress is saved incrementally every 50 files, so crashes don't lose work.

        With a time budget the run stops between files once it is spent, and
        ends normally. A file counts as done only when the saved state says so,
        so whatever it didn't reach, including removals of deleted files, the
        next run picks up. Stats still refresh for the people it touched, since
        their files won't come round again. ``last_run`` reports what was left.

        Args:
            force: If True, reindex all files regardless of modification time
            skip_summaries: If True, skip LLM summary generation for faster indexing
            time_budget_seconds: How long the run may take. None runs to completion.
            max_files: Index at most this many changed files, leaving the rest
                for a later run.
            newest_first: If True, index changed files by descending modification
                time. Otherwise they follow vault walk order.
            light_pass: If True, run as a pass a later run completes: see
                ``index_file``. A file that could not be read stays unrecorded so
                a later run retries it.
            max_deletions: Remove at most this many deleted files from the index,
                leaving the rest for a later run.

        Returns:
            Number of files indexed
        """
        deadline = None if time_budget_seconds is None else self._clock() + time_budget_seconds

        def out_of_time() -> bool:
            return deadline is not None and self._clock() >= deadline

        # Always load existing state - even in force mode, we track progress
        # so we can resume if interrupted
        index_state = self._load_index_state()

        tag_keys: dict = index_state.get(TAG_STATE_KEY) or {}
        index_state[TAG_STATE_KEY] = tag_keys
        files_to_index, deleted_files, current_tag_key, file_count = self._find_changes(index_state, force)

        if force:
            already_done = file_count - len(files_to_index)
            if already_done > 0:
                logger.info(f"RESUMING FULL REINDEX: {len(files_to_index)} remaining, {already_done} already indexed")
            else:
                logger.info(f"FULL REINDEX: {file_count} files")
        else:
            logger.info(f"Incremental index: {len(files_to_index)} changed, {len(deleted_files)} deleted, {file_count - len(files_to_index)} unchanged")

        save_interval = 10  # Save state every N files (small to survive timeouts)

        # Delete removed files from index, saving progress so an interrupted run
        # does not repeat them
        pruned = 0
        prunes_attempted = 0
        deleted_this_run = sorted(deleted_files)
        if max_deletions is not None and len(deleted_this_run) > max_deletions:
            logger.info(f"Removing {max_deletions} of {len(deleted_this_run)} deleted files this run; the rest wait for a later run")
            deleted_this_run = deleted_this_run[:max_deletions]
        for file_path in deleted_this_run:
            if out_of_time():
                break
            prunes_attempted += 1
            try:
                self.delete_file(file_path)
                # Remove from state
                index_state.pop(file_path, None)
                tag_keys.pop(file_path, None)
                pruned += 1
                logger.info(f"Removed deleted file from index: {file_path}")
                if pruned % save_interval == 0:
                    self._save_index_state(index_state)
            except Exception as e:
                logger.error(f"Failed to remove {file_path} from index: {e}")
        if prunes_attempted:
            self._save_index_state(index_state)

        if time_budget_seconds is not None:
            files_to_index = self._ordered_for_budget(files_to_index)
        elif newest_first:
            files_to_index = sorted(files_to_index, key=lambda item: item[1], reverse=True)
        files_changed = len(files_to_index)
        if max_files is not None and files_changed > max_files:
            logger.info(f"Indexing {max_files} of {files_changed} changed files this run; the rest wait for a later run")
            files_to_index = files_to_index[:max_files]

        # Index changed files, saving progress incrementally
        count = 0
        attempted = 0
        all_affected_person_ids: set[str] = set()

        for file_path, mtime in files_to_index:
            if out_of_time():
                break
            attempted += 1
            try:
                affected_ids = self.index_file(
                    file_path, skip_stats_refresh=True, skip_summaries=skip_summaries, light_pass=light_pass
                )
                # With skip_stats_refresh, None means the file was never indexed.
                if light_pass and affected_ids is None:
                    continue
                if affected_ids:
                    all_affected_person_ids.update(affected_ids)

                # Update state for this file
                index_state[file_path] = mtime
                if current_tag_key[file_path] is None:
                    tag_keys.pop(file_path, None)
                else:
                    tag_keys[file_path] = current_tag_key[file_path]
                count += 1

                # Save progress and clean up memory periodically
                if count % save_interval == 0:
                    self._save_index_state(index_state)
                    gc.collect()  # Prevent memory bloat during long indexing runs
                    try:
                        import torch
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                    except Exception:
                        pass
                    logger.info(f"  Indexed {count}/{len(files_to_index)} files (progress saved)...")

            except Exception as e:
                logger.error(f"Failed to index {file_path}: {e}")

        # Final save
        self._save_index_state(index_state)
        logger.info(f"Indexed {count} files (final save)")

        files_remaining = (files_changed - attempted) + (len(deleted_files) - prunes_attempted)
        stopped_early = files_remaining > 0
        self.last_run = {
            "files_pruned": pruned,
            "files_remaining": files_remaining,
            "stopped_early": stopped_early,
        }
        if stopped_early and deadline is not None:
            logger.info(f"Time budget reached: {files_remaining} files remain for the next run")

        # Batch refresh all affected person stats ONCE at the end
        if all_affected_person_ids:
            from api.services.person_stats import refresh_person_stats
            logger.info(f"Refreshing stats for {len(all_affected_person_ids)} affected people...")
            refresh_person_stats(list(all_affected_person_ids))

        return count

    def index_file(
        self,
        file_path: str,
        skip_stats_refresh: bool = False,
        skip_summaries: bool = False,
        light_pass: bool = False,
    ) -> set[str] | None:
        """
        Index a single file.

        Args:
            file_path: Path to the file
            skip_stats_refresh: If True, return affected person IDs instead of refreshing.
                               Used by index_all() to batch refresh at the end.
            skip_summaries: If True, skip LLM summary generation for faster indexing.
            light_pass: If True, queue the note for the ``vault_summaries`` sync
                source instead of generating a summary, and leave the task index
                to the task file watcher in the API server.

        Returns:
            Set of affected person IDs if skip_stats_refresh=True and the file was
            indexed, else None
        """
        path = Path(file_path)
        if not path.exists() or not path.suffix == ".md":
            return
        if self._is_excluded_from_index(path):
            return

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to read {file_path}: {e}")
            return

        # Reindex task files for the task manager cache
        if "LifeOS/Tasks/" in str(path) and not light_pass:
            try:
                from api.services.task_manager import get_task_manager
                get_task_manager().reindex_file(str(path))
            except Exception as e:
                logger.warning(f"Task reindex failed for {file_path}: {e}")

        # Extract frontmatter
        frontmatter, body = extract_frontmatter(content)

        # Determine if Granola note
        is_granola = (
            "granola_id" in frontmatter or
            "wispr_id" in frontmatter or
            "Granola" in str(path)
        )

        # Chunk the document
        chunks = chunk_document(content, is_granola=is_granola)

        # People named in the body, frontmatter, and tags
        note_tags = normalize_tags(frontmatter.get("tags"))
        all_people = people_in_note(frontmatter, body)

        # Sync to v2 people system if available. Bulk content stays searchable
        # below but says nothing about contact with anyone.
        affected_person_ids: set[str] = set()
        if HAS_V2_PEOPLE and self._no_people_reason(path, frontmatter) is None:
            try:
                from datetime import timezone
                note_date_str = self._extract_note_date(path, frontmatter, body)

                if note_date_str:
                    # Dated note: use extracted date
                    note_date = datetime.strptime(note_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                else:
                    # Undated note: fall back to filesystem mtime so the
                    # interaction lands on the timeline at a meaningful date
                    # (when the user last touched the note) rather than
                    # 1970-01-01.
                    #
                    # Caveat: a bulk migration / restore-from-backup can give
                    # thousands of files the same recent mtime even though
                    # the underlying notes are years old. Gate mtime use
                    # behind `LIFEOS_VAULT_MTIME_TRUSTED_AFTER=YYYY-MM-DD`:
                    # only mtimes strictly later than that cutoff are used,
                    # everything else falls back to UNDATED_SENTINEL. If the
                    # env var is unset, mtime is never trusted (legacy
                    # behavior, safe default).
                    note_date = _resolve_undated_note_date(path)
                    if note_date == UNDATED_SENTINEL:
                        logger.debug(f"Undated note (sentinel): {path.name}")
                    else:
                        logger.debug(f"Undated note (mtime {note_date.date()}): {path.name}")

                affected_person_ids = self._sync_people_to_v2(
                    path, all_people, note_date, is_granola, frontmatter=frontmatter, body=body
                )

                # Refresh stats for affected people (unless caller will batch refresh)
                if affected_person_ids and not skip_stats_refresh:
                    from api.services.person_stats import refresh_person_stats
                    refresh_person_stats(list(affected_person_ids))

            except Exception as e:
                logger.warning(f"Failed to sync people to v2 for {file_path}: {e}")

        # Build metadata - use resolve() to get real path (handles symlinks like /var -> /private/var)
        metadata = {
            "file_path": str(path.resolve()),
            "file_name": path.name,
            "modified_date": self._extract_note_date(path, frontmatter, body),
            "note_type": self._infer_note_type(path),
            "people": all_people,
            "tags": [t for t in note_tags if not is_people_tag(t)],
            "granola_id": frontmatter.get("granola_id"),  # For context generation
            "wispr_id": frontmatter.get("wispr_id"),  # For context generation
        }

        # Add contextual prefixes to chunks (P9.1 - boosts retrieval accuracy)
        chunks = add_context_to_chunks(chunks, path, metadata, tags=self._tag_for(path))

        # Before the old summary leaves BM25, so the queue drain can't leave a
        # summary of the old text behind.
        if light_pass:
            # Not caught: a note that was not queued must not be recorded as
            # indexed, or nothing would ever summarize it.
            from api.services.summarizer import queue_summary
            queue_summary(file_path, path.name, body)
        else:
            self._invalidate_queued_summary(file_path)

        # Update in vector store (handles deletion of old chunks)
        self.vector_store.update_document(chunks, metadata)

        # Update in BM25 index for keyword search
        # First delete all existing chunks (numbered + summary) for this file —
        # delete_document only matches one exact id, which left stale chunks
        # behind whenever the count shrank or the vault path changed.
        self.bm25_index.delete_by_path(str(path.resolve()))
        # Add each chunk to BM25
        for i, chunk in enumerate(chunks):
            doc_id = f"{path.resolve()}_{i}"
            self.bm25_index.add_document(
                doc_id=doc_id,
                content=chunk.get("content", ""),
                file_name=path.name,
                people=all_people if all_people else None,
                modified_date=metadata["modified_date"]
            )

        # Generate document summary for discovery queries (P9.4)
        # Uses tiered summarization: SKIP for archives, HIGH for important content
        if not skip_summaries and not light_pass:
            try:
                from api.services.summarizer import (
                    generate_summary, get_summary_tier, SummaryTier, add_summary_failure
                )

                tier = get_summary_tier(file_path)

                if tier == SummaryTier.SKIP:
                    logger.debug(f"Skipping summary for {file_path} (tier: SKIP)")
                else:
                    summary, success = generate_summary(body, path.name)
                    if success and summary:
                        summary_id = f"{path.resolve()}::summary"
                        summary_content = f"Document summary for {path.name}: {summary}"

                        # Add summary chunk to BM25 (for keyword search)
                        self.bm25_index.add_document(
                            doc_id=summary_id,
                            content=summary_content,
                            file_name=path.name,
                            people=all_people if all_people else None,
                            modified_date=metadata["modified_date"]
                        )

                        logger.debug(f"Generated summary for {file_path} (tier: {tier.value})")
                    elif not success:
                        # Track failure for retry at end of indexing
                        add_summary_failure(file_path, path.name)
            except Exception as e:
                logger.warning(f"Summary generation failed for {file_path}: {e}")

        logger.debug(f"Indexed {file_path} with {len(chunks)} chunks")

        # Return affected person IDs for batch refresh (when called from index_all)
        if skip_stats_refresh:
            return affected_person_ids
        return None

    def delete_file(self, file_path: str) -> None:
        """
        Remove a file from the index.

        Args:
            file_path: Path to the deleted file
        """
        self._invalidate_queued_summary(file_path)

        # Use os.path.realpath to resolve symlinks (e.g., /var -> /private/var on macOS)
        # This works even for non-existent files
        real_path = os.path.realpath(file_path)
        self.vector_store.delete_document(real_path)
        # delete_by_path drops numbered chunks + the summary in one query.
        self.bm25_index.delete_by_path(real_path)

        logger.debug(f"Deleted {file_path} from index (resolved: {real_path})")

    @staticmethod
    def _invalidate_queued_summary(file_path: str) -> None:
        """See ``summarizer.invalidate_queued_summary``. Never stops indexing."""
        from api.services.summarizer import invalidate_queued_summary

        try:
            invalidate_queued_summary(file_path)
        except Exception as e:
            logger.warning(f"Could not update the summary queue for {file_path}: {e}")

    def _extract_note_date(self, path: Path, frontmatter: dict, body: str = "") -> str:
        """Extract the note date; see `api.utils.date_parser.extract_note_date`."""
        from api.utils.date_parser import extract_note_date

        return extract_note_date(path, frontmatter, body)

    def _infer_note_type(self, path: Path) -> str:
        """Infer the note type from the file's vault-relative path."""
        from api.services.vault_listing import infer_note_type

        try:
            rel = path.resolve().relative_to(self.vault_path.resolve())
        except ValueError:
            return "Other"
        return infer_note_type(rel)

    def _rel_path(self, path: Path) -> str | None:
        """A note's vault-relative POSIX path, or None if it's outside the vault."""
        try:
            return path.resolve().relative_to(self.vault_path.resolve()).as_posix()
        except ValueError:
            return None

    def _no_people_reason(self, path: Path, frontmatter: dict) -> str | None:
        from config.settings import settings

        return no_people_reason(self._rel_path(path), frontmatter, settings.vault_no_people_paths)

    def _is_excluded_from_index(self, path: Path) -> bool:
        """Whether a file sits under a _-prefixed folder of the vault."""
        try:
            return in_underscore_dir(path.relative_to(self.vault_path))
        except ValueError:
            return False

    def _get_mention_resolver(self, resolver, interaction_store) -> "MentionResolver | None":
        """A resolver over the current people, rebuilt once it has aged out.

        None when it can't be built, in which case mentions needing context stay
        as they are until a later run.
        """
        age = time.monotonic() - self._mention_resolver_built_at
        if self._mention_resolver is not None and age < MENTION_RESOLVER_MAX_AGE_SECONDS:
            return self._mention_resolver
        try:
            from config.settings import settings

            crm_db = resolver.store.db_path
            self._mention_resolver = MentionResolver(
                resolver.store.get_all(),
                crm_db=crm_db,
                interaction_db=interaction_store.db_path,
                vault_root=self.vault_path,
                overrides=load_link_overrides(crm_db),
                my_person_id=settings.my_person_id or None,
                owner_name=settings.user_name or None,
            )
            self._mention_resolver_built_at = time.monotonic()
        except Exception as e:
            logger.warning(f"Mention resolver unavailable: {e}")
            self._mention_resolver = None
        return self._mention_resolver

    def _sync_people_to_v2(
        self,
        path: Path,
        people: list[str],
        note_date: datetime,
        is_granola: bool = False,
        frontmatter: dict | None = None,
        body: str = "",
    ) -> set[str]:
        """
        Resolve a note's people and record their mentions.

        A full name resolves through the entity resolver, which may create the
        person. A first name or initial is ambiguous: it goes to the mention
        resolver, which reads it in the note's context, links only on a clear
        winner, and creates no one. Each mention gets a source entity (for the
        split UI), unlinked when it means no one clearly. A mention a human
        confirmed or rejected keeps that decision.

        A note holds one interaction, which goes to the first person who
        resolves, full names first. It follows the note: a holder the note doesn't
        resolve to gives it up to whoever comes first, or it goes if no one does.
        A holder whose link a human confirmed keeps it, as does a visible one-word
        person the note still names.

        Note: PersonEntity stats (mention_count) are updated via refresh_person_stats()
        after sync completes, not manually here.

        Args:
            path: Path to the note file
            people: List of extracted person names
            note_date: Date of the note (for interaction timestamp)
            is_granola: Whether this is a Granola meeting note
            frontmatter: Parsed frontmatter, read as context for ambiguous mentions
            body: Note body, read as context for ambiguous mentions

        Returns:
            Set of affected person IDs (for stats refresh)
        """
        affected_person_ids: set[str] = set()
        if not HAS_V2_PEOPLE:
            return affected_person_ids

        # Use injected stores if available, otherwise fall back to singletons
        resolver = self._entity_resolver or get_entity_resolver()
        interaction_store = self._interaction_store or get_interaction_store()
        source_entity_store = self._source_entity_store or get_source_entity_store()
        file_path_str = str(path.resolve())

        logger.debug(
            f"Syncing {len(people)} people to v2 from {path.name}"
        )

        ambiguous = sorted({name for name in people if is_ambiguous_mention(name)})
        full_names = sorted(set(people) - set(ambiguous))

        resolved: list = []
        complete = True
        co_mentioned_ids: set[str] = set()
        for person_name in full_names:
            try:
                # Resolve person with context path for domain boosting
                # e.g., file in Work/ folders will boost work domain (LIFEOS_WORK_DOMAIN) matches
                result = resolver.resolve(
                    name=person_name,
                    context_path=file_path_str,
                    create_if_missing=True,
                )
                if not result:
                    logger.debug(f"Could not resolve person: {person_name}")
                    continue
                co_mentioned_ids.add(result.entity.id)
                resolved.append(self._record_mention(
                    path, person_name, result.entity, result.confidence, result.match_type,
                    note_date, is_granola, resolver, interaction_store, source_entity_store,
                    affected_person_ids,
                ))
            except Exception as e:
                complete = False
                logger.warning(f"Failed to sync person '{person_name}' to v2: {e}")

        if not ambiguous:
            if complete:
                self._settle_note_interaction(
                    path, resolved, ambiguous, is_granola, resolver, interaction_store, source_entity_store,
                    affected_person_ids,
                )
            return affected_person_ids

        mention_resolver = self._get_mention_resolver(resolver, interaction_store)
        if mention_resolver is None:
            return affected_person_ids

        context = self._note_context(path, frontmatter or {}, body, note_date, is_granola, co_mentioned_ids)
        for person_name in ambiguous:
            try:
                decision = mention_resolver.resolve(person_name, context)
                resolved.append(self._record_mention(
                    path, person_name, decision.entity, decision.confidence, decision.match_type,
                    note_date, is_granola, resolver, interaction_store, source_entity_store,
                    affected_person_ids,
                ))
            except Exception as e:
                complete = False
                logger.warning(f"Failed to sync person '{person_name}' to v2: {e}")

        if complete:
            self._settle_note_interaction(
                path, resolved, ambiguous, is_granola, resolver, interaction_store, source_entity_store,
                affected_person_ids,
            )
        return affected_person_ids

    def _settle_note_interaction(
        self, path, resolved, ambiguous, is_granola, resolver, interaction_store, source_entity_store,
        affected_person_ids,
    ) -> None:
        """Make a note's interaction agree with who the note resolves to now.

        Args:
            resolved: Each mention's person or None, in resolution order
            ambiguous: The note's one-word mentions
        """
        source_type = "granola" if is_granola else "vault"
        file_path_str = str(path.resolve())
        current = interaction_store.get_by_source(source_type, file_path_str)
        people = [entity for entity in resolved if entity is not None]
        if current is None or current.person_id in {entity.id for entity in people}:
            return
        if source_entity_store.has_confirmed_note_link(source_type, file_path_str, current.person_id):
            return
        holder = resolver.store.get_by_id(current.person_id)
        if (
            holder and not holder.hidden
            and holder.canonical_name.strip().casefold() in {name.casefold() for name in ambiguous}
        ):
            return

        affected_person_ids.add(current.person_id)
        if not people:
            interaction_store.delete(current.id)
            return
        interaction_store.reassign(current.id, people[0].id)
        self._credit_note(people[0], file_path_str, source_type, resolver, affected_person_ids)

    def _credit_note(self, entity, file_path_str, source_type, resolver, affected_person_ids) -> None:
        """Record on a person that this note's interaction is theirs."""
        affected_person_ids.add(entity.id)
        if file_path_str not in entity.related_notes:
            entity.related_notes.append(file_path_str)
        if source_type not in entity.sources:
            entity.sources.append(source_type)
        # Persist entity changes (stats updated via refresh_person_stats)
        resolver.store.update(entity)

    def _note_context(self, path, frontmatter, body, note_date, is_granola, co_mentioned_ids):
        """Build the context a note offers for reading its ambiguous mentions."""
        from config.employment_history import get_employment_history

        tag = self._tag_for(path)
        return build_note_context(
            self._rel_path(path) or path.name,
            frontmatter,
            body,
            note_date=None if note_date == UNDATED_SENTINEL else note_date.date(),
            tag_domain=getattr(tag, "domain", None),
            employment=get_employment_history(),
            co_mentioned_ids=co_mentioned_ids,
            is_granola=is_granola,
        )

    def _record_mention(
        self,
        path,
        person_name,
        entity,
        confidence,
        match_type,
        note_date,
        is_granola,
        resolver,
        interaction_store,
        source_entity_store,
        affected_person_ids,
    ):
        """Write one mention's source entity and, if it links, the note's interaction.

        Returns:
            The person the mention resolves to, or None
        """
        file_path_str = str(path.resolve())
        note_title = path.stem  # filename without .md
        source_metadata = {
            "note_title": note_title,
            "is_granola": is_granola,
        }
        create_source_entity = create_granola_source_entity if is_granola else create_vault_source_entity
        source_entity = create_source_entity(
            file_path=file_path_str,
            person_name=person_name,
            observed_at=note_date,
            metadata=source_metadata,
        )

        existing = source_entity_store.get_by_source(source_entity.source_type, source_entity.source_id)
        if existing is not None and existing.link_status != LINK_STATUS_AUTO:
            confirmed = existing.link_status == LINK_STATUS_CONFIRMED and existing.canonical_person_id
            entity = resolver.store.get_by_id(existing.canonical_person_id) if confirmed else None
        else:
            if entity is not None:
                source_entity.canonical_person_id = entity.id
                source_entity.link_confidence = confidence
                source_entity.link_method = match_type
                source_entity.linked_at = datetime.now(timezone.utc)
            # Add or update (handles duplicates on re-index)
            source_entity_store.add_or_update(source_entity)

        if entity is None:
            return None

        # Add interaction (avoiding duplicates on re-index)
        interaction = create_vault_interaction(
            person_id=entity.id,
            file_path=file_path_str,
            title=note_title,
            timestamp=note_date,
            snippet=None,  # Could extract first N chars if desired
            is_granola=is_granola,
        )
        _, was_added = interaction_store.add_if_not_exists(interaction)

        if was_added:
            self._credit_note(
                entity, file_path_str, "granola" if is_granola else "vault", resolver, affected_person_ids
            )
            logger.debug(
                f"Created interaction for {entity.display_name} "
                f"({match_type}, conf={confidence:.2f})"
            )
        return entity
