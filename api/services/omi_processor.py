"""
Omi Events Processor for LifeOS.

Processes the Omi/Events folder every 5 minutes, automatically
classifying and moving event notes to the appropriate folder.

Destinations:
- /Personal/Omi - general personal events (catchall)
- /Personal/Relationship/Omi - romantic/relationship discussions
- /Personal/Finance/Omi - personal finance (mortgage, loans, etc.)
- /Personal/Self-Improvement/Therapy and coaching/Omi - therapy sessions
- /Work/ML/Meetings/Omi - work meetings
"""
import re
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional

import frontmatter

logger = logging.getLogger(__name__)


# Categories from Omi that map to work
WORK_CATEGORIES = {"work", "business", "finance", "technology"}

# Categories that are personal (may or may not be therapy - need content patterns to confirm)
# Note: psychology, romantic, parenting can all be therapy sessions OR regular personal events
PERSONAL_CATEGORIES = {"romantic", "parenting", "personal", "family", "social", "psychology"}

# Categories that MIGHT indicate therapy but need content confirmation
# (psychology could be therapy OR just a personal discussion about feelings)
THERAPY_HINT_CATEGORIES = {"psychology", "romantic", "parenting"}

def _get_therapist_patterns() -> list[str]:
    """Get therapist name patterns from settings."""
    try:
        from config.settings import settings
        if settings.therapist_patterns:
            return [rf"\b{name}\b" for name in settings.therapist_patterns.split("|")]
    except Exception:
        pass
    return []


# Content patterns that CONFIRM therapy (required to route to therapy folder)
# Category alone is NOT sufficient - we need these content signals
# Base patterns + dynamically loaded therapist names from settings
THERAPY_CONTENT_PATTERNS = [
    r"\btherapy\s*session\b",
    r"\btherapist\b",
    r"\bwith\s+(?:their|my|the)\s+therapist\b",
    r"\bcouples?\s*therapy\b",
    r"\bindividual\s*therapy\b",
    r"\btherapy\b.*\b(?:appointment|meeting)\b",
    r"\bspeaker\s*\d+.*therapist\b",
    r"\btalks?\s+with\s+(?:their|my)\s+therapist\b",
] + _get_therapist_patterns()

# Content patterns that suggest work (used to confirm/override category)
WORK_CONTENT_PATTERNS = [
    r"\bteam\s*(?:meeting|aligns?|reviews?)\b",
    r"\bbudget(?:ing)?\b",
    r"\b(?:quarterly|annual)\s*(?:planning|review)\b",
    r"\bOKR\b",
    r"\broadmap\b",
    r"\bsprint\b",
    r"\b(?:1-1|one-on-one)\s*(?:meeting|with)\b",
    r"\bperformance\s*review\b",
    r"\bproject\s*(?:update|status)\b",
    r"\b(?:R&D|engineering|product)\s*(?:team|meeting)\b",
]

# Content patterns that suggest personal (generic - catchall)
PERSONAL_CONTENT_PATTERNS = [
    r"\bpersonal\b",
]

def _get_partner_pattern() -> list[str]:
    """Get partner name pattern from settings."""
    try:
        from config.settings import settings
        if settings.partner_name:
            return [rf"\b{settings.partner_name}\b"]
    except Exception:
        pass
    return []


# Content patterns for RELATIONSHIP discussions
# Routes to Personal/Relationship/Omi
# Base patterns + dynamically loaded partner name from settings
RELATIONSHIP_CONTENT_PATTERNS = [
    r"\bromantic\s*partner",
    r"\brelationship\b.*\b(?:conflict|discussion|issue)\b",
    r"\bco-?parenting\b",
    r"\bpartner\b.*\b(?:feels?|said|wants?)\b",
    r"\bcouple\b.*\b(?:conflict|discussion|works?)\b",
    r"\bcommunication\s*(?:conflict|issue|style)\b",
] + _get_partner_pattern()

# Content patterns for PERSONAL finance (overrides business/finance category)
# These are personal matters even if Omi categorizes them as "business" or "finance"
PERSONAL_FINANCE_PATTERNS = [
    r"\bmortgage\b",
    r"\bhome\s*(?:loan|purchase|buying)\b",
    r"\bhouse\s*(?:loan|purchase|buying)\b",
    r"\bVA\s*loan\b",
    r"\bFHA\s*loan\b",
    r"\bdown\s*payment\b",
    r"\breal\s*estate\s*(?:agent|purchase)\b",
    r"\bhome\s*(?:inspection|appraisal)\b",
    r"\bclosing\s*costs?\b",
    r"\bpersonal\s*finance\b",
    r"\bretirement\s*(?:account|savings|planning)\b",
    r"\b401k\b",
    r"\bIRA\b",
    r"\btax\s*(?:return|filing|refund)\b",
]


class OmiProcessor:
    """
    Process event notes from Omi/Events folder.

    Runs every 5 minutes (configurable) to classify and move notes
    to appropriate destinations based on category and content patterns.
    """

    def __init__(self, vault_path: str, interval_seconds: int = 300):
        """
        Initialize Omi processor.

        Args:
            vault_path: Path to Obsidian vault
            interval_seconds: How often to check for new files (default: 300 = 5 minutes)
        """
        self.vault_path = Path(vault_path)
        self.omi_events_path = self.vault_path / "Omi" / "Events"
        self.interval_seconds = interval_seconds
        self._timer: Optional[threading.Timer] = None
        self._running = False
        self._lock = threading.Lock()

        # Destination folders (all under vault_path)
        # Work path loaded from settings
        from config.settings import settings
        work_path = settings.current_work_path.rstrip("/") if settings.current_work_path else "Work"
        relationship_folder = settings.relationship_folder if settings.relationship_folder else "Relationship"

        self.dest_personal = "Personal/Omi"
        self.dest_relationship = f"Personal/{relationship_folder}/Omi"
        self.dest_finance = "Personal/Finance/Omi"
        self.dest_therapy = "Personal/Self-Improvement/Therapy and coaching/Omi"
        self.dest_work = f"{work_path}/Meetings/Omi"

    def find_files_by_omi_id(self, omi_id: str, exclude_path: Optional[Path] = None) -> list[Path]:
        """
        Find all files in the vault with the given omi_id.

        Args:
            omi_id: The Omi ID to search for
            exclude_path: Path to exclude from results (typically the source file)

        Returns:
            List of paths to files with matching omi_id
        """
        matches = []
        for md_file in self.vault_path.rglob("*.md"):
            if exclude_path and md_file == exclude_path:
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
                post = frontmatter.loads(content)
                if post.metadata.get("omi_id") == omi_id:
                    matches.append(md_file)
            except Exception:
                continue
        return matches

    def delete_duplicates_by_omi_id(self, omi_id: str, keep_path: Optional[Path] = None) -> int:
        """
        Delete all duplicate files with the given omi_id except the one to keep.

        Args:
            omi_id: The Omi ID to search for
            keep_path: Path to keep (don't delete this one)

        Returns:
            Number of duplicates deleted
        """
        duplicates = self.find_files_by_omi_id(omi_id, exclude_path=keep_path)
        deleted = 0
        for dup_path in duplicates:
            try:
                dup_path.unlink()
                logger.info(f"Deleted duplicate omi file: {dup_path}")
                deleted += 1
            except Exception as e:
                logger.error(f"Failed to delete duplicate {dup_path}: {e}")
        return deleted

    def _matches_patterns(self, text: str, patterns: list[str]) -> bool:
        """Check if text matches any of the given patterns."""
        text_lower = text.lower()
        for pattern in patterns:
            if re.search(pattern, text_lower, re.IGNORECASE):
                return True
        return False

    def classify_note(self, content: str, metadata: dict) -> tuple[str, list[str], str]:
        """
        Classify a note based on category and content patterns.

        The category field is a strong signal but not wholly determinative.
        Content patterns can confirm or override the category-based classification.

        IMPORTANT: Therapy detection requires content patterns - category alone is
        NOT sufficient. Categories like psychology, romantic, parenting can be
        therapy sessions OR regular personal conversations.

        Args:
            content: Full note content
            metadata: Frontmatter metadata dict

        Returns:
            Tuple of (destination_folder, tags, classification_rationale)
        """
        category = metadata.get("category", "").lower()
        full_text = content.lower()

        # Check for therapy content patterns (REQUIRED for therapy classification)
        has_therapy_content = self._matches_patterns(full_text, THERAPY_CONTENT_PATTERNS)

        # Check for relationship content
        has_relationship_content = self._matches_patterns(full_text, RELATIONSHIP_CONTENT_PATTERNS)

        # Check for personal finance (overrides business/finance category)
        has_personal_finance = self._matches_patterns(full_text, PERSONAL_FINANCE_PATTERNS)

        # Check for work signals
        is_work_category = category in WORK_CATEGORIES
        has_work_content = self._matches_patterns(full_text, WORK_CONTENT_PATTERNS)

        # Check for personal category
        is_personal_category = category in PERSONAL_CATEGORIES

        # Check if category hints at possible therapy (but needs content confirmation)
        is_therapy_hint_category = category in THERAPY_HINT_CATEGORIES

        # Check for romantic/relationship category
        is_relationship_category = category in {"romantic", "parenting"}

        # Classification logic with rationale
        #
        # Priority 1: Therapy detection
        # CRITICAL: Therapy requires content patterns - category alone is NOT enough
        if has_therapy_content:
            return (
                self.dest_therapy,
                ["omi", "therapy", "personal"],
                f"Therapy content patterns detected (category: '{category}')"
            )

        # Priority 2: Personal finance detection
        # Overrides business/finance category - mortgage, home loans, etc. are personal
        if has_personal_finance:
            return (
                self.dest_finance,
                ["omi", "personal", "finance"],
                "Personal finance content (mortgage, home loan, etc.)"
            )

        # Priority 3: Relationship detection
        # romantic/parenting category OR relationship content patterns
        if has_relationship_content or is_relationship_category:
            return (
                self.dest_relationship,
                ["omi", "personal", "relationship"],
                f"Relationship content (category: '{category}')"
            )

        # Priority 4: Work detection
        # - work category + work content = definitely work
        # - work category alone = likely work
        # - work content alone (without personal category) = likely work
        if is_work_category and has_work_content:
            return (
                self.dest_work,
                ["omi", "meeting", "work"],
                f"Category '{category}' + work content patterns"
            )

        if is_work_category:
            return (
                self.dest_work,
                ["omi", "meeting", "work"],
                f"Category '{category}' suggests work"
            )

        if has_work_content and not is_personal_category:
            return (
                self.dest_work,
                ["omi", "meeting", "work"],
                "Content contains work patterns"
            )

        # Priority 5: Personal catchall
        # - psychology without therapy content = Personal/Omi
        # - unknown/other category = personal (safest default)
        return (
            self.dest_personal,
            ["omi", "personal"],
            f"Default personal (category: '{category}')"
        )

    def update_frontmatter(
        self,
        content: str,
        tags: list[str],
    ) -> str:
        """
        Update frontmatter with proper LifeOS fields.

        Preserves Omi-specific fields (omi_id, category, etc.).
        Adds: modified, tags, type.
        """
        try:
            post = frontmatter.loads(content)
        except Exception:
            post = frontmatter.Post(content)

        # Set modified date
        post.metadata["modified"] = datetime.now().strftime("%Y-%m-%d")

        # Merge tags (preserve existing, add new)
        existing_tags = post.metadata.get("tags", [])
        if isinstance(existing_tags, str):
            existing_tags = [existing_tags]
        merged_tags = list(set(existing_tags + tags))
        post.metadata["tags"] = merged_tags

        # Set type
        post.metadata["type"] = "omi-event"

        return frontmatter.dumps(post)

    def process_file(self, file_path: str) -> Optional[str]:
        """
        Process a single Omi file.

        Args:
            file_path: Path to the file

        Returns:
            New path if moved, None if skipped
        """
        path = Path(file_path)

        if not path.exists():
            logger.warning(f"File no longer exists: {file_path}")
            return None

        if not path.suffix == ".md":
            return None

        # Check if file is in Omi/Events folder
        try:
            path.relative_to(self.omi_events_path)
        except ValueError:
            logger.debug(f"File not in Omi/Events folder, skipping: {file_path}")
            return None

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to read {file_path}: {e}")
            return None

        # Parse frontmatter
        try:
            post = frontmatter.loads(content)
            metadata = post.metadata
        except Exception:
            metadata = {}

        # Extract omi_id for duplicate detection
        omi_id = metadata.get("omi_id")

        # If this omi_id already exists elsewhere, delete the duplicates first
        if omi_id:
            deleted = self.delete_duplicates_by_omi_id(omi_id, keep_path=path)
            if deleted > 0:
                logger.info(f"Deleted {deleted} existing duplicate(s) for omi_id {omi_id}")

        # Classify the note
        destination, tags, rationale = self.classify_note(content, metadata)

        # Update frontmatter
        updated_content = self.update_frontmatter(content, tags)

        # Determine destination path
        dest_folder = self.vault_path / destination
        dest_folder.mkdir(parents=True, exist_ok=True)
        dest_path = dest_folder / path.name

        # Handle filename conflicts
        if dest_path.exists() and dest_path != path:
            try:
                existing_content = dest_path.read_text(encoding="utf-8")
                existing_post = frontmatter.loads(existing_content)
                existing_omi_id = existing_post.metadata.get("omi_id")
                if existing_omi_id == omi_id and omi_id is not None:
                    # Same omi_id - this is a duplicate, delete it
                    dest_path.unlink()
                    logger.info(f"Removed existing duplicate at destination: {dest_path}")
                else:
                    # Different file, need to rename
                    base = dest_path.stem
                    suffix = dest_path.suffix
                    counter = 1
                    while dest_path.exists():
                        dest_path = dest_folder / f"{base}_{counter}{suffix}"
                        counter += 1
            except Exception:
                base = dest_path.stem
                suffix = dest_path.suffix
                counter = 1
                while dest_path.exists():
                    dest_path = dest_folder / f"{base}_{counter}{suffix}"
                    counter += 1

        # Write updated content to destination
        try:
            dest_path.write_text(updated_content, encoding="utf-8")
            logger.info(f"Wrote updated content to: {dest_path}")
        except Exception as e:
            logger.error(f"Failed to write to {dest_path}: {e}")
            return None

        # Remove original file (if different from destination)
        if path != dest_path:
            try:
                path.unlink()
                logger.info(f"Removed original file: {path}")
            except Exception as e:
                logger.error(f"Failed to remove original {path}: {e}")
                logger.error(f"Rolling back: deleting newly written file {dest_path}")
                try:
                    dest_path.unlink()
                except Exception:
                    pass
                return None

        logger.info(f"Processed: {path.name} -> {destination} ({rationale})")
        return str(dest_path)

    def reclassify_file(self, file_path: str) -> Optional[str]:
        """
        Reclassify and move a file that may have been incorrectly categorized.

        Unlike process_file (which only processes files in Omi/Events), this can
        process files anywhere in the vault and move them to the correct location.

        Args:
            file_path: Path to the file

        Returns:
            New path if moved, None if file should stay where it is
        """
        path = Path(file_path)

        if not path.exists():
            logger.warning(f"File no longer exists: {file_path}")
            return None

        if not path.suffix == ".md":
            return None

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to read {file_path}: {e}")
            return None

        # Check if this is an Omi file (has omi_id in frontmatter)
        omi_id = None
        try:
            post = frontmatter.loads(content)
            omi_id = post.metadata.get("omi_id")
            if not omi_id:
                logger.debug(f"Not an Omi file, skipping: {file_path}")
                return None
            metadata = post.metadata
        except Exception:
            return None

        # Classify the note
        destination, tags, rationale = self.classify_note(content, metadata)

        # Determine correct destination path
        dest_folder = self.vault_path / destination
        dest_path = dest_folder / path.name

        # Check if already in correct location
        try:
            current_dest = path.parent.relative_to(self.vault_path)
            if str(current_dest) == destination:
                # Already in correct location - but still check for duplicates elsewhere
                deleted = self.delete_duplicates_by_omi_id(omi_id, keep_path=path)
                if deleted > 0:
                    logger.info(f"Deleted {deleted} duplicate(s) for omi_id {omi_id}, kept {path}")
                    return str(path)
                logger.debug(f"File already in correct location: {file_path}")
                return None
        except ValueError:
            pass

        # Delete any existing duplicates with the same omi_id (except current file)
        deleted = self.delete_duplicates_by_omi_id(omi_id, keep_path=path)
        if deleted > 0:
            logger.info(f"Deleted {deleted} existing duplicate(s) for omi_id {omi_id}")

        # Update frontmatter
        updated_content = self.update_frontmatter(content, tags)

        # Create destination folder if needed
        dest_folder.mkdir(parents=True, exist_ok=True)

        # Handle filename conflicts
        if dest_path.exists() and dest_path != path:
            try:
                existing_content = dest_path.read_text(encoding="utf-8")
                existing_post = frontmatter.loads(existing_content)
                existing_omi_id = existing_post.metadata.get("omi_id")
                if existing_omi_id == omi_id:
                    dest_path.unlink()
                    logger.info(f"Removed existing duplicate at destination: {dest_path}")
                else:
                    base = dest_path.stem
                    suffix = dest_path.suffix
                    counter = 1
                    while dest_path.exists():
                        dest_path = dest_folder / f"{base}_{counter}{suffix}"
                        counter += 1
            except Exception:
                base = dest_path.stem
                suffix = dest_path.suffix
                counter = 1
                while dest_path.exists():
                    dest_path = dest_folder / f"{base}_{counter}{suffix}"
                    counter += 1

        # Write updated content to destination
        try:
            dest_path.write_text(updated_content, encoding="utf-8")
            logger.info(f"Wrote reclassified file to: {dest_path}")
        except Exception as e:
            logger.error(f"Failed to write to {dest_path}: {e}")
            return None

        # Remove original file
        if path != dest_path:
            try:
                path.unlink()
                logger.info(f"Removed original file: {path}")
            except Exception as e:
                logger.error(f"Failed to remove original {path}: {e}")
                logger.error(f"Rolling back: deleting newly written file {dest_path}")
                try:
                    dest_path.unlink()
                except Exception:
                    pass
                return None

        logger.info(f"Reclassified: {path.name} -> {destination} ({rationale})")
        return str(dest_path)

    def reclassify_folder(self, folder_path: str) -> dict:
        """
        Scan a folder and reclassify any Omi files that are in the wrong location.

        Args:
            folder_path: Path to folder to scan

        Returns:
            Dict with 'reclassified', 'failed', 'skipped' counts and 'moves' list
        """
        results = {
            "reclassified": 0,
            "failed": 0,
            "skipped": 0,
            "moves": []
        }

        folder = Path(folder_path)
        if not folder.exists():
            logger.warning(f"Folder does not exist: {folder_path}")
            return results

        for md_file in folder.rglob("*.md"):
            try:
                new_path = self.reclassify_file(str(md_file))
                if new_path:
                    results["reclassified"] += 1
                    results["moves"].append({
                        "original": str(md_file),
                        "destination": new_path
                    })
                else:
                    results["skipped"] += 1
            except Exception as e:
                logger.error(f"Failed to reclassify {md_file}: {e}")
                results["failed"] += 1

        logger.info(
            f"Reclassification complete: {results['reclassified']} moved, "
            f"{results['skipped']} skipped, {results['failed']} failed"
        )
        return results

    def find_all_duplicates(self) -> dict[str, list[Path]]:
        """
        Find all duplicate Omi files in the vault (files with the same omi_id).

        Returns:
            Dict mapping omi_id to list of file paths (only for IDs with 2+ files)
        """
        omi_files: dict[str, list[Path]] = {}

        for md_file in self.vault_path.rglob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                post = frontmatter.loads(content)
                omi_id = post.metadata.get("omi_id")
                if omi_id:
                    if omi_id not in omi_files:
                        omi_files[omi_id] = []
                    omi_files[omi_id].append(md_file)
            except Exception:
                continue

        return {oid: paths for oid, paths in omi_files.items() if len(paths) > 1}

    def deduplicate_all(self) -> dict:
        """
        Find and remove all duplicate Omi files in the vault.

        For each set of duplicates, keeps the file in the best location
        (based on classification) and deletes the rest.

        Returns:
            Dict with 'duplicates_found', 'files_deleted', 'files_kept' and 'details' list
        """
        results = {
            "duplicates_found": 0,
            "files_deleted": 0,
            "files_kept": 0,
            "details": []
        }

        duplicates = self.find_all_duplicates()
        results["duplicates_found"] = len(duplicates)

        for omi_id, paths in duplicates.items():
            best_path = None
            best_score = -1

            for path in paths:
                try:
                    content = path.read_text(encoding="utf-8")
                    post = frontmatter.loads(content)
                    destination, _, _ = self.classify_note(content, post.metadata)

                    try:
                        current_folder = str(path.parent.relative_to(self.vault_path))
                        if current_folder == destination:
                            score = 2
                        elif current_folder.startswith(destination):
                            score = 1
                        else:
                            score = 0
                    except ValueError:
                        score = 0

                    if score > best_score:
                        best_score = score
                        best_path = path

                except Exception as e:
                    logger.error(f"Error evaluating {path}: {e}")
                    continue

            if best_path is None:
                best_path = paths[0]

            detail = {
                "omi_id": omi_id,
                "kept": str(best_path),
                "deleted": []
            }

            for path in paths:
                if path != best_path:
                    try:
                        path.unlink()
                        detail["deleted"].append(str(path))
                        results["files_deleted"] += 1
                        logger.info(f"Deleted duplicate: {path}")
                    except Exception as e:
                        logger.error(f"Failed to delete duplicate {path}: {e}")

            results["files_kept"] += 1
            results["details"].append(detail)

        logger.info(
            f"Deduplication complete: {results['duplicates_found']} duplicate sets found, "
            f"{results['files_deleted']} files deleted, {results['files_kept']} files kept"
        )
        return results

    def process_backlog(self) -> dict:
        """
        Process all existing files in the Omi/Events folder.

        Returns:
            Dict with 'processed', 'failed', 'skipped' counts and 'moves' list
        """
        results = {
            "processed": 0,
            "failed": 0,
            "skipped": 0,
            "moves": []
        }

        if not self.omi_events_path.exists():
            logger.warning(f"Omi/Events folder does not exist: {self.omi_events_path}")
            return results

        for md_file in self.omi_events_path.glob("*.md"):
            try:
                new_path = self.process_file(str(md_file))
                if new_path:
                    results["processed"] += 1
                    results["moves"].append({
                        "original": str(md_file),
                        "destination": new_path
                    })
                else:
                    results["skipped"] += 1
            except Exception as e:
                logger.error(f"Failed to process {md_file}: {e}")
                results["failed"] += 1

        logger.info(
            f"Backlog processed: {results['processed']} moved, "
            f"{results['skipped']} skipped, {results['failed']} failed"
        )
        return results

    def _run_cycle(self):
        """Run one processing cycle and schedule the next."""
        if not self._running:
            return

        logger.debug("Running Omi processor cycle")
        try:
            results = self.process_backlog()
            if results["processed"] > 0:
                logger.info(f"Omi cycle: processed {results['processed']} files")
        except Exception as e:
            logger.error(f"Omi processor cycle failed: {e}")
            # Record failure for nightly batch report
            try:
                from api.services.notifications import record_failure
                record_failure("Omi processor", str(e))
            except Exception as notify_err:
                logger.error(f"Failed to record Omi failure: {notify_err}")

        # Schedule next run
        if self._running:
            self._timer = threading.Timer(self.interval_seconds, self._run_cycle)
            self._timer.daemon = True
            self._timer.start()

    def start(self) -> None:
        """Start the processor (runs every interval_seconds)."""
        with self._lock:
            if self._running:
                logger.debug("Omi processor already running")
                return

            if not self.omi_events_path.exists():
                logger.warning(f"Omi/Events folder does not exist: {self.omi_events_path}")
                return

            self._running = True

            # Run immediately on start
            logger.info(f"Starting Omi processor (interval: {self.interval_seconds}s)")
            self._run_cycle()

    def stop(self) -> None:
        """Stop the processor."""
        with self._lock:
            self._running = False
            if self._timer:
                self._timer.cancel()
                self._timer = None
            logger.info("Stopped Omi processor")

    @property
    def is_running(self) -> bool:
        """Check if processor is running."""
        return self._running


# Singleton instance
_processor_instance: Optional[OmiProcessor] = None


def get_omi_processor(vault_path: str) -> OmiProcessor:
    """Get or create the Omi processor singleton."""
    global _processor_instance
    if _processor_instance is None:
        _processor_instance = OmiProcessor(vault_path)
    return _processor_instance
