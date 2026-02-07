"""
Indexer service for LifeOS.

Watches the Obsidian vault for file changes and indexes content to ChromaDB.
Supports incremental indexing based on file modification times.
"""
import gc
import os
import re
import time
import json
import threading
import logging
from pathlib import Path
from datetime import datetime, timezone
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

from api.services.chunker import chunk_document, extract_frontmatter, add_context_to_chunks
from api.services.vectorstore import VectorStore
from api.services.bm25_index import BM25Index
from api.services.people import extract_people_from_text

# V2 People System integration
try:
    from api.services.entity_resolver import EntityResolver, get_entity_resolver
    from api.services.interaction_store import (
        InteractionStore,
        get_interaction_store,
        create_vault_interaction,
        build_obsidian_link,
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


class VaultEventHandler(FileSystemEventHandler):
    """Handle file system events in the vault."""

    def __init__(self, indexer: "IndexerService"):
        self.indexer = indexer
        self._debounce_timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _debounced_process(self, file_path: str, action: str):
        """Process file change with debouncing."""
        with self._lock:
            # Cancel existing timer for this file
            if file_path in self._debounce_timers:
                self._debounce_timers[file_path].cancel()

            # Create new timer
            def process():
                with self._lock:
                    self._debounce_timers.pop(file_path, None)
                if action == "delete":
                    self.indexer.delete_file(file_path)
                else:
                    self.indexer.index_file(file_path)

            timer = threading.Timer(1.0, process)  # 1 second debounce
            self._debounce_timers[file_path] = timer
            timer.start()

    def on_created(self, event: FileSystemEvent):
        if not event.is_directory and event.src_path.endswith(".md"):
            logger.info(f"File created: {event.src_path}")
            self._debounced_process(event.src_path, "index")

    def on_modified(self, event: FileSystemEvent):
        if not event.is_directory and event.src_path.endswith(".md"):
            logger.info(f"File modified: {event.src_path}")
            self._debounced_process(event.src_path, "index")

    def on_deleted(self, event: FileSystemEvent):
        if not event.is_directory and event.src_path.endswith(".md"):
            logger.info(f"File deleted: {event.src_path}")
            self._debounced_process(event.src_path, "delete")

    def on_moved(self, event: FileSystemEvent):
        if not event.is_directory:
            if hasattr(event, 'src_path') and event.src_path.endswith(".md"):
                logger.info(f"File moved from: {event.src_path}")
                self._debounced_process(event.src_path, "delete")
            if hasattr(event, 'dest_path') and event.dest_path.endswith(".md"):
                logger.info(f"File moved to: {event.dest_path}")
                self._debounced_process(event.dest_path, "index")


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
        db_path: str = "./data/chromadb"
    ):
        """
        Initialize indexer.

        Args:
            vault_path: Path to Obsidian vault
            db_path: Path to ChromaDB database
        """
        self.vault_path = Path(vault_path)
        self.db_path = Path(db_path)

        # Initialize vector store
        self.vector_store = VectorStore()

        # Initialize BM25 keyword index
        self.bm25_index = BM25Index()

        # File watcher
        self._observer: Observer | None = None
        self._watching = False

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

        # Determine which files need indexing
        files_to_index = []
        for file_path, mtime in current_files.items():
            prev_mtime = index_state.get(file_path)
            if force:
                # In force mode, reindex if not yet indexed in this run
                # (allows resuming a force reindex after crash)
                if prev_mtime is None or prev_mtime < mtime:
                    files_to_index.append((file_path, mtime))
            else:
                # Normal incremental: only if file changed since last index
                if prev_mtime is None or mtime > prev_mtime:
                    files_to_index.append((file_path, mtime))

        # Determine deleted files (in old state but not in current files)
        deleted_files = set(index_state.keys()) - set(current_files.keys())

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
                count += 1

                # Save progress and clean up memory periodically
                if count % save_interval == 0:
                    self._save_index_state(index_state)
                    gc.collect()  # Prevent memory bloat during long indexing runs
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
        all_people = list(set(extracted_people + frontmatter_people))

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
                    # Undated note: use sentinel date so it still appears in counts
                    # and can be shown in an "Undated" section of timeline
                    note_date = UNDATED_SENTINEL
                    logger.debug(f"Undated note (using sentinel date): {path.name}")

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
            "tags": frontmatter.get("tags", []),
            "granola_id": frontmatter.get("granola_id"),  # For context generation
        }

        # Add contextual prefixes to chunks (P9.1 - improves retrieval by 35-50%)
        chunks = add_context_to_chunks(chunks, path, metadata)

        # Update in vector store (handles deletion of old chunks)
        self.vector_store.update_document(chunks, metadata)

        # Update in BM25 index for keyword search
        # First delete any existing chunks for this file
        self.bm25_index.delete_document(str(path.resolve()))
        # Add each chunk to BM25
        for i, chunk in enumerate(chunks):
            doc_id = f"{path.resolve()}_{i}"
            self.bm25_index.add_document(
                doc_id=doc_id,
                content=chunk.get("content", ""),
                file_name=path.name,
                people=all_people if all_people else None
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
                            people=all_people if all_people else None
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
        self.bm25_index.delete_document(real_path)

        # Also delete summary chunk if exists
        summary_id = f"{real_path}::summary"
        self.bm25_index.delete_document(summary_id)

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
                _, body = extract_frontmatter(content)

                summary = retry_summary(body, file_name)
                if summary:
                    summary_id = f"{path.resolve()}::summary"
                    summary_content = f"Document summary for {file_name}: {summary}"

                    # Add to BM25 index
                    self.bm25_index.add_document(
                        doc_id=summary_id,
                        content=summary_content,
                        file_name=file_name
                    )
                    success_count += 1

            except Exception as e:
                logger.warning(f"Retry failed for {file_path}: {e}")

        # Clear failures list after processing
        clear_summary_failures()
        logger.info(f"Summary retry complete: {success_count}/{len(failed_files)} succeeded")

        return success_count

    def _extract_note_date(self, path: Path, frontmatter: dict, body: str = "") -> str:
        """
        Extract note date using priority cascade.

        Priority:
        1. Filename patterns (YYYY-MM-DD, YYYYMMDD)
        2. Frontmatter fields: created, date, created_at, creation_date
        3. Body text: "Created: ...", "Date: ..."
        4. Return empty string (NO file timestamp fallback)

        Args:
            path: Path to the file
            frontmatter: Parsed frontmatter dict
            body: Note body content (for searching date patterns)

        Returns:
            ISO format date string or empty string if no date found
        """
        from api.utils.date_parser import parse_note_date

        filename = path.stem  # filename without extension

        # 1. Look for YYYY-MM-DD pattern in filename
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", filename)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

        # 2. Look for YYYYMMDD pattern in filename (e.g., "Meeting 20250925.md")
        match = re.search(r"(\d{4})(\d{2})(\d{2})", filename)
        if match:
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
            if 2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
                return f"{year:04d}-{month:02d}-{day:02d}"

        # 3. Frontmatter fields
        for field in ['created', 'date', 'created_at', 'creation_date']:
            if field in frontmatter:
                value = frontmatter[field]
                if isinstance(value, datetime):
                    return value.strftime("%Y-%m-%d")
                if isinstance(value, str):
                    parsed = parse_note_date(value)
                    if parsed:
                        return parsed

        # 4. Body text patterns (first 2000 chars)
        body_sample = body[:2000] if body else ""
        for pattern in [r"Created:\s*(.+?)(?:\n|$)", r"Date:\s*(.+?)(?:\n|$)"]:
            match = re.search(pattern, body_sample, re.IGNORECASE)
            if match:
                parsed = parse_note_date(match.group(1).strip())
                if parsed:
                    return parsed

        # 5. No reliable date found - return empty string
        # The vector store will give these a neutral recency score
        return ""

    def _infer_note_type(self, path: Path) -> str:
        """
        Infer note type from folder path.

        Args:
            path: Path to the file

        Returns:
            Note type string
        """
        path_str = str(path).lower()
        # Also check case-sensitive for ML
        path_str_orig = str(path)

        # ML folder = current job (high priority)
        if "/ML/" in path_str_orig or "\\ML\\" in path_str_orig:
            return "ML"
        elif "granola" in path_str:
            return "Granola"
        elif "personal" in path_str:
            return "Personal"
        elif "work" in path_str:
            return "Work"
        elif "lifeos" in path_str:
            return "LifeOS"
        else:
            return "Other"

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

        resolver = get_entity_resolver()
        interaction_store = get_interaction_store()
        source_entity_store = get_source_entity_store()
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
