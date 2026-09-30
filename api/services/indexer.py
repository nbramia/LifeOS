"""
Indexer service for LifeOS.

Watches the Obsidian vault for file changes and indexes content to ChromaDB.
Supports incremental indexing based on file modification times.
"""
import gc
import os
import json
import threading
import logging
from pathlib import Path
from datetime import datetime, timezone
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

from api.services.chunker import chunk_document, extract_frontmatter, add_context_to_chunks, normalize_tags, tag_phrase_key
from api.services.vectorstore import VectorStore, is_people_tag
from api.services.bm25_index import BM25Index
from api.services.people import extract_people_from_text, people_from_tags

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
        get_source_entity_store,
        create_vault_source_entity,
        create_granola_source_entity,
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


class VaultEventHandler(FileSystemEventHandler):
    """Handle file system events in the vault with global batch debouncing.

    Collects all file changes within a window and processes them as a single
    batch, preventing sustained indexing load from rapid file operations
    (e.g., bulk imports or external tools moving multiple files).
    """

    _BATCH_DELAY = 5.0  # seconds - wait for activity to settle before processing

    def __init__(self, indexer: "IndexerService"):
        self.indexer = indexer
        self._pending: dict[str, str] = {}  # file_path -> action ("index" or "delete")
        self._batch_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _queue(self, file_path: str, action: str):
        """Queue a file change and reset the batch timer."""
        with self._lock:
            self._pending[file_path] = action  # latest action wins
            if self._batch_timer:
                self._batch_timer.cancel()
            self._batch_timer = threading.Timer(self._BATCH_DELAY, self._flush)
            self._batch_timer.daemon = True
            self._batch_timer.start()

    def _flush(self):
        """Process all pending file changes as a batch."""
        with self._lock:
            batch = dict(self._pending)
            self._pending.clear()
            self._batch_timer = None

        if not batch:
            return

        logger.info(f"Processing batch of {len(batch)} file changes")
        for file_path, action in batch.items():
            try:
                if action == "delete":
                    self.indexer.delete_file(file_path)
                else:
                    self.indexer.index_file(file_path)
            except Exception as e:
                logger.error(f"Failed to {action} {file_path}: {e}")

    def on_created(self, event: FileSystemEvent):
        if not event.is_directory and event.src_path.endswith(".md"):
            logger.info(f"File created: {event.src_path}")
            self._queue(event.src_path, "index")

    def on_modified(self, event: FileSystemEvent):
        if not event.is_directory and event.src_path.endswith(".md"):
            logger.info(f"File modified: {event.src_path}")
            self._queue(event.src_path, "index")

    def on_deleted(self, event: FileSystemEvent):
        if not event.is_directory and event.src_path.endswith(".md"):
            logger.info(f"File deleted: {event.src_path}")
            self._queue(event.src_path, "delete")

    def on_moved(self, event: FileSystemEvent):
        if not event.is_directory:
            if hasattr(event, 'src_path') and event.src_path.endswith(".md"):
                logger.info(f"File moved from: {event.src_path}")
                self._queue(event.src_path, "delete")
            if hasattr(event, 'dest_path') and event.dest_path.endswith(".md"):
                logger.info(f"File moved to: {event.dest_path}")
                self._queue(event.dest_path, "index")


# Reserved key in the index state: file path -> tag tuple used at index time.
TAG_STATE_KEY = "__tag_phrase_keys__"


class IndexerService:
    """
    Main indexer service.

    Handles indexing of Obsidian vault files to ChromaDB.
    Supports incremental indexing based on file modification times.
    """

    # State file for tracking indexed files
    INDEX_STATE_FILE = "data/vault_index_state.json"

    def __init__(
        self,
        vault_path: str,
        db_path: str = "./data/chromadb",
        interaction_store=None,
        source_entity_store=None,
        entity_resolver=None,
        tag_store=None,
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
        """
        self.vault_path = Path(vault_path)
        self.db_path = Path(db_path)

        # Store injected dependencies (or None to use singletons)
        self._interaction_store = interaction_store
        self._source_entity_store = source_entity_store
        self._entity_resolver = entity_resolver
        self._tag_store = tag_store
        self._tag_store_failed = False

        # Initialize vector store
        self.vector_store = VectorStore()

        # Initialize BM25 keyword index
        self.bm25_index = BM25Index()

        # File watcher
        self._observer: Observer | None = None
        self._watching = False

    def _tag_for(self, path: Path):
        """The file's TagRecord, or None when the store or the row is missing.

        Rows are keyed by vault-relative POSIX path, the same key
        ``scripts/sync_vault_tag.py`` writes.
        """
        if self._tag_store is None:
            if self._tag_store_failed:
                return None
            try:
                from api.services.vault_tag_store import VaultTagStore
                self._tag_store = VaultTagStore()
            except Exception as e:
                logger.warning(f"Tag store unavailable, indexing without tag phrases: {e}")
                self._tag_store_failed = True
                return None
        try:
            key = Path(path).relative_to(self.vault_path).as_posix()
            return self._tag_store.get(key)
        except Exception:
            return None

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
        state_path.write_text(json.dumps(state, indent=2))

    def index_all(self, force: bool = False, skip_summaries: bool = False) -> int:
        """
        Index markdown files in the vault.

        Uses incremental indexing by default - only indexes files that have
        changed since the last index run. Use force=True to reindex everything.

        Progress is saved incrementally every 50 files, so crashes don't lose work.

        Args:
            force: If True, reindex all files regardless of modification time
            skip_summaries: If True, skip LLM summary generation for faster indexing

        Returns:
            Number of files indexed
        """
        # Always load existing state - even in force mode, we track progress
        # so we can resume if interrupted
        index_state = self._load_index_state()

        # Get all current markdown files with their mtimes
        all_md_files = list(self.vault_path.rglob("*.md"))
        current_files = {str(f): f.stat().st_mtime for f in all_md_files}

        # Tag tuples used at index time live under a reserved key beside the
        # per-file mtimes. A file whose current tuple differs is re-indexed
        # even when its bytes did not change; no row and no phrase is None on
        # both sides, so untagged files are never re-indexed for it.
        tag_keys: dict = index_state.get(TAG_STATE_KEY) or {}
        index_state[TAG_STATE_KEY] = tag_keys
        current_tag_key: dict[str, list | None] = {}

        # Determine which files need indexing
        files_to_index = []
        for file_path, mtime in current_files.items():
            prev_mtime = index_state.get(file_path)
            key = tag_phrase_key(self._tag_for(Path(file_path)))
            current_tag_key[file_path] = list(key) if key else None
            tags_changed = current_tag_key[file_path] != tag_keys.get(file_path)
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

        if force:
            already_done = len(all_md_files) - len(files_to_index)
            if already_done > 0:
                logger.info(f"RESUMING FULL REINDEX: {len(files_to_index)} remaining, {already_done} already indexed")
            else:
                logger.info(f"FULL REINDEX: {len(all_md_files)} files")
        else:
            logger.info(f"Incremental index: {len(files_to_index)} changed, {len(deleted_files)} deleted, {len(all_md_files) - len(files_to_index)} unchanged")

        # Delete removed files from index
        for file_path in deleted_files:
            try:
                self.delete_file(file_path)
                # Remove from state
                index_state.pop(file_path, None)
                tag_keys.pop(file_path, None)
                logger.info(f"Removed deleted file from index: {file_path}")
            except Exception as e:
                logger.error(f"Failed to remove {file_path} from index: {e}")

        # Index changed files, saving progress incrementally
        count = 0
        all_affected_person_ids: set[str] = set()
        save_interval = 10  # Save state every N files (small to survive timeouts)

        for file_path, mtime in files_to_index:
            try:
                affected_ids = self.index_file(file_path, skip_stats_refresh=True, skip_summaries=skip_summaries)
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

        # Retry failed summaries with simpler prompt and longer timeout
        if not skip_summaries:
            retry_count = self._retry_failed_summaries()
            if retry_count > 0:
                logger.info(f"Retried {retry_count} failed summaries")
            gc.collect()  # Clean up after retry phase

        # Batch refresh all affected person stats ONCE at the end
        if all_affected_person_ids:
            from api.services.person_stats import refresh_person_stats
            logger.info(f"Refreshing stats for {len(all_affected_person_ids)} affected people...")
            refresh_person_stats(list(all_affected_person_ids))

        return count

    def index_file(self, file_path: str, skip_stats_refresh: bool = False, skip_summaries: bool = False) -> set[str] | None:
        """
        Index a single file.

        Args:
            file_path: Path to the file
            skip_stats_refresh: If True, return affected person IDs instead of refreshing.
                               Used by index_all() to batch refresh at the end.
            skip_summaries: If True, skip LLM summary generation for faster indexing.

        Returns:
            Set of affected person IDs if skip_stats_refresh=True, else None
        """
        path = Path(file_path)
        if not path.exists() or not path.suffix == ".md":
            return

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to read {file_path}: {e}")
            return

        # Reindex task files for the task manager cache
        if "LifeOS/Tasks/" in str(path):
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
            "Granola" in str(path)
        )

        # Chunk the document
        chunks = chunk_document(content, is_granola=is_granola)

        # Extract people from content (in addition to frontmatter)
        extracted_people = extract_people_from_text(body)
        frontmatter_people = frontmatter.get("people", [])

        # Merge people lists (unique)
        note_tags = normalize_tags(frontmatter.get("tags"))
        all_people = list(set(extracted_people + frontmatter_people + people_from_tags(note_tags)))

        # Sync to v2 people system if available
        affected_person_ids: set[str] = set()
        if HAS_V2_PEOPLE and all_people:
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

                affected_person_ids = self._sync_people_to_v2(path, all_people, note_date, is_granola)

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
        }

        # Add contextual prefixes to chunks (P9.1 - boosts retrieval accuracy)
        chunks = add_context_to_chunks(chunks, path, metadata, tags=self._tag_for(path))

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
        if not skip_summaries:
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
        # Use os.path.realpath to resolve symlinks (e.g., /var -> /private/var on macOS)
        # This works even for non-existent files
        real_path = os.path.realpath(file_path)
        self.vector_store.delete_document(real_path)
        # delete_by_path drops numbered chunks + the summary in one query.
        self.bm25_index.delete_by_path(real_path)

        logger.debug(f"Deleted {file_path} from index (resolved: {real_path})")

    def _retry_failed_summaries(self) -> int:
        """
        Retry summary generation for files that failed in the first pass.

        Uses a simpler prompt and longer timeout for better success rate.

        Returns:
            Number of files successfully retried
        """
        from api.services.summarizer import (
            load_summary_failures, clear_summary_failures, retry_summary
        )

        failures = load_summary_failures()
        failed_files = failures.get("files", [])

        if not failed_files:
            return 0

        logger.info(f"Retrying {len(failed_files)} failed summaries with simpler prompt...")

        success_count = 0
        for failure in failed_files:
            file_path = failure["file_path"]
            file_name = failure["file_name"]

            try:
                path = Path(file_path)
                if not path.exists():
                    continue

                content = path.read_text(encoding="utf-8")
                # Extract body (skip frontmatter)
                from api.services.chunker import extract_frontmatter
                frontmatter, body = extract_frontmatter(content)

                summary = retry_summary(body, file_name)
                if summary:
                    summary_id = f"{path.resolve()}::summary"
                    summary_content = f"Document summary for {file_name}: {summary}"

                    # Add to BM25 index
                    self.bm25_index.add_document(
                        doc_id=summary_id,
                        content=summary_content,
                        file_name=file_name,
                        modified_date=self._extract_note_date(path, frontmatter, body)
                    )
                    success_count += 1

            except Exception as e:
                logger.warning(f"Retry failed for {file_path}: {e}")

        # Clear failures list after processing
        clear_summary_failures()
        logger.info(f"Summary retry complete: {success_count}/{len(failed_files)} succeeded")

        return success_count

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

    def _sync_people_to_v2(
        self,
        path: Path,
        people: list[str],
        note_date: datetime,
        is_granola: bool = False,
    ) -> set[str]:
        """
        Resolve extracted people and create vault mention interactions and source entities.

        Hooks into the v2 people system to:
        1. Resolve each person name to a PersonEntity (creating if needed)
        2. Create an interaction record for the vault mention
        3. Create a source entity for the vault/granola mention (for split UI)

        Note: PersonEntity stats (mention_count) are updated via refresh_person_stats()
        after sync completes, not manually here.

        Args:
            path: Path to the note file
            people: List of extracted person names
            note_date: Date of the note (for interaction timestamp)
            is_granola: Whether this is a Granola meeting note

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
        note_title = path.stem  # filename without .md

        logger.debug(
            f"Syncing {len(people)} people to v2 from {path.name}"
        )

        for person_name in people:
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

                entity = result.entity

                # Create vault interaction
                interaction = create_vault_interaction(
                    person_id=entity.id,
                    file_path=file_path_str,
                    title=note_title,
                    timestamp=note_date,
                    snippet=None,  # Could extract first N chars if desired
                    is_granola=is_granola,
                )

                # Add interaction (avoiding duplicates on re-index)
                _, was_added = interaction_store.add_if_not_exists(interaction)

                # Track affected person for stats refresh
                if was_added:
                    affected_person_ids.add(entity.id)

                # Create source entity for split UI visibility
                # This uses add_or_update so re-indexing won't create duplicates
                source_metadata = {
                    "note_title": note_title,
                    "is_granola": is_granola,
                }
                if is_granola:
                    source_entity = create_granola_source_entity(
                        file_path=file_path_str,
                        person_name=person_name,
                        observed_at=note_date,
                        metadata=source_metadata,
                    )
                else:
                    source_entity = create_vault_source_entity(
                        file_path=file_path_str,
                        person_name=person_name,
                        observed_at=note_date,
                        metadata=source_metadata,
                    )

                # Link to the resolved person
                source_entity.canonical_person_id = entity.id
                source_entity.link_confidence = result.confidence
                source_entity.link_method = result.match_type
                source_entity.linked_at = datetime.now(timezone.utc)

                # Add or update (handles duplicates on re-index)
                source_entity_store.add_or_update(source_entity)

                if was_added:
                    # Update related_notes (not a count, just a list)
                    if file_path_str not in entity.related_notes:
                        entity.related_notes.append(file_path_str)

                    # Update last_seen
                    if entity.last_seen is None or note_date > entity.last_seen:
                        entity.last_seen = note_date

                    # Add vault/granola to sources if not present
                    source_type = "granola" if is_granola else "vault"
                    if source_type not in entity.sources:
                        entity.sources.append(source_type)

                    # Persist entity changes (stats updated via refresh_person_stats)
                    resolver.store.update(entity)

                    logger.debug(
                        f"Created interaction for {entity.display_name} "
                        f"({result.match_type}, conf={result.confidence:.2f})"
                    )

            except Exception as e:
                logger.warning(
                    f"Failed to sync person '{person_name}' to v2: {e}"
                )

        return affected_person_ids

    def start_watching(self) -> None:
        """Start watching the vault for changes."""
        if self._watching:
            return

        self._observer = Observer()
        event_handler = VaultEventHandler(self)
        self._observer.schedule(
            event_handler,
            str(self.vault_path),
            recursive=True
        )
        self._observer.start()
        self._watching = True
        logger.info(f"Started watching {self.vault_path}")

    def stop(self) -> None:
        """Stop watching and cleanup."""
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        self._watching = False
        logger.info("Stopped watching")

    @property
    def is_watching(self) -> bool:
        """Check if currently watching."""
        return self._watching
