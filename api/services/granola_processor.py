"""
Granola Inbox Processor for LifeOS.

Processes the Granola/ folder every 5 minutes, automatically
classifying and moving meeting notes to the appropriate folder.

Per PRD P0.1:
- Watches Granola/ folder for new/modified files
- Classifies by content patterns
- Moves to appropriate destination folder
- Updates frontmatter with proper tags
- Logs all moves with rationale
"""
import re
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional

import frontmatter

logger = logging.getLogger(__name__)


def _get_work_path() -> str:
    """Get current work path from settings."""
    try:
        from config.settings import settings
        return settings.current_work_path.rstrip("/") if settings.current_work_path else "Work"
    except Exception:
        return "Work"


def _get_therapist_patterns() -> list[str]:
    """Get therapist name patterns from settings."""
    try:
        from config.settings import settings
        if settings.therapist_patterns:
            return [rf"\b{name}\b" for name in settings.therapist_patterns.split("|")]
    except Exception:
        pass
    return []


def _build_filename_rules() -> list[dict]:
    """Build filename rules dynamically using settings."""
    work_path = _get_work_path()
    therapist_patterns = _get_therapist_patterns()

    rules = [
        {
            "name": "finance_filename",
            "patterns": [
                r"\bmoney\s*meeting\b", r"\bbudget\b", r"\bfinance\b",
                r"\bfinancial\b", r"\brevenue\b"
            ],
            "destination": f"{work_path}/Finance",
            "tags": ["meeting", "work", "finance"]
        },
    ]

    # Add therapy filename rule if therapist patterns configured
    therapy_patterns = [r"\btherapy\b"] + therapist_patterns
    if therapy_patterns:
        rules.append({
            "name": "therapy_filename",
            "patterns": therapy_patterns,
            "destination": "Personal/Self-Improvement/Therapy and coaching",
            "tags": ["meeting", "therapy", "personal"]
        })

    return rules


def _build_content_rules() -> list[dict]:
    """Build content classification rules dynamically using settings."""
    work_path = _get_work_path()
    therapist_patterns = _get_therapist_patterns()

    # Base therapy patterns
    therapy_content_patterns = [
        r"\btherapy\s*session\b", r"\btherapist\b",
        r"\bcouples\s*therapy\b", r"\bindividual\s*therapy\b"
    ] + therapist_patterns

    return [
        {
            "name": "therapy",
            "patterns": therapy_content_patterns,
            "destination": "Personal/Self-Improvement/Therapy and coaching",
            "tags": ["meeting", "therapy", "personal"]
        },
        # NOTE: Finance classification removed from content rules - was too aggressive.
        # Meetings that merely mentioned "budget planning" in a section were being
        # miscategorized. Finance is now detected via FILENAME_RULES only (if the
        # meeting filename contains "budget", "finance", etc., it's about finance).
        {
            "name": "hiring",
            "patterns": [
                r"\bjob\s*interview\b", r"\bhiring\s*decision\b",
                r"\bjob\s*description\b", r"\bcandidate\s*interview\b",
                r"\brecruitment\s*for\s*(?:position|role)\b", r"\bresume\s*review\b",
                r"\binterview\s*panel\b", r"\binterview\s*feedback\b"
            ],
            "destination": f"{work_path}/People/Hiring",
            "tags": ["meeting", "work", "hiring"]
        },
        {
            "name": "strategy",
            "patterns": [
                r"\bstrategy\s*meeting\b", r"\bstrategic\s*planning\b",
                r"\bquarterly\s*planning\b", r"\bgoal\s*setting\b",
                r"\bOKR\s*review\b", r"\broadmap\s*planning\b"
            ],
            "destination": f"{work_path}/Strategy and planning",
            "tags": ["meeting", "work", "strategy"]
        },
        {
            "name": "union",
            "patterns": [
                r"\bunion\s*meeting\b", r"\bunion\s*steward\b",
                r"\bcollective\s*bargaining\b", r"\bgrievance\b"
            ],
            "destination": f"{work_path}/People/Union",
            "tags": ["meeting", "work"]
        },
    ]


# Build rules at module load time
FILENAME_RULES = _build_filename_rules()
CLASSIFICATION_RULES = _build_content_rules()


def _get_personal_relationship_rule() -> Optional[dict]:
    """Build personal relationship rule from settings if configured."""
    try:
        from config.settings import settings
        if settings.personal_relationship_patterns:
            patterns = [
                rf"\b{p}\b" for p in settings.personal_relationship_patterns.split("|")
            ]
            return {
                "name": "personal_relationship",
                "patterns": patterns,
                "destination": "Personal/Relationship",
                "tags": ["meeting", "personal", "relationship"]
            }
    except Exception:
        pass
    return None


def _build_classification_rules() -> list[dict]:
    """Build classification rules, including config-based patterns."""
    rules = list(CLASSIFICATION_RULES)  # Copy static rules

    # Add personal relationship rule if configured
    personal_rule = _get_personal_relationship_rule()
    if personal_rule:
        rules.append(personal_rule)

    return rules


# Known colleagues for 1-1 detection
# Load from settings if configured, otherwise use empty list
def _get_current_colleagues() -> list[str]:
    """Get current colleagues from settings."""
    try:
        from config.settings import settings
        return settings.current_colleagues if settings.current_colleagues else []
    except Exception:
        return []


CURRENT_COLLEAGUES = _get_current_colleagues()
EFFECTIVE_CLASSIFICATION_RULES = _build_classification_rules()


class GranolaProcessor:
    """
    Process meeting notes from Granola inbox folder.

    Runs every 5 minutes (configurable) to classify and move notes
    to appropriate destinations based on content patterns defined in the PRD.
    """

    def __init__(self, vault_path: str, interval_seconds: int = 300):
        """
        Initialize Granola processor.

        Args:
            vault_path: Path to Obsidian vault
            interval_seconds: How often to check for new files (default: 300 = 5 minutes)
        """
        self.vault_path = Path(vault_path)
        self.granola_path = self.vault_path / "Granola"
        self.interval_seconds = interval_seconds
        self._timer: Optional[threading.Timer] = None
        self._running = False
        self._lock = threading.Lock()

    def find_files_by_granola_id(self, granola_id: str, exclude_path: Optional[Path] = None) -> list[Path]:
        """
        Find all files in the vault with the given granola_id.

        Args:
            granola_id: The Granola ID to search for
            exclude_path: Path to exclude from results (typically the source file)

        Returns:
            List of paths to files with matching granola_id
        """
        matches = []
        for md_file in self.vault_path.rglob("*.md"):
            if exclude_path and md_file == exclude_path:
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
                post = frontmatter.loads(content)
                if post.metadata.get("granola_id") == granola_id:
                    matches.append(md_file)
            except Exception:
                continue
        return matches

    def delete_duplicates_by_granola_id(self, granola_id: str, keep_path: Optional[Path] = None) -> int:
        """
        Delete all duplicate files with the given granola_id except the one to keep.

        Args:
            granola_id: The Granola ID to search for
            keep_path: Path to keep (don't delete this one)

        Returns:
            Number of duplicates deleted
        """
        duplicates = self.find_files_by_granola_id(granola_id, exclude_path=keep_path)
        deleted = 0
        for dup_path in duplicates:
            try:
                dup_path.unlink()
                logger.info(f"Deleted duplicate granola file: {dup_path}")
                deleted += 1
            except Exception as e:
                logger.error(f"Failed to delete duplicate {dup_path}: {e}")
        return deleted

    def classify_note(self, content: str, filename: str) -> tuple[str, list[str], str]:
        """
        Classify a note based on filename and content patterns.

        Priority order:
        1. Filename-based rules (highest priority)
        2. 1-1 meetings with known colleagues
        3. Content-based rules
        4. Default (Work/Meetings)

        Args:
            content: Full note content
            filename: Name of the file

        Returns:
            Tuple of (destination_folder, tags, classification_rationale)
        """
        from config.settings import settings
        work_path = _get_work_path()
        user_name = settings.user_name.lower() if settings.user_name else "user"

        filename_lower = filename.lower()
        content_lower = content.lower()

        # 1. Check filename-based rules first (highest priority)
        for rule in FILENAME_RULES:
            for pattern in rule["patterns"]:
                if re.search(pattern, filename_lower, re.IGNORECASE):
                    rationale = f"Filename matched '{pattern}' for category '{rule['name']}'"
                    return rule["destination"], rule["tags"], rationale

        # 2. Check for 1-1 meetings with colleagues (based on filename)
        for person in CURRENT_COLLEAGUES:
            person_lower = person.lower()
            patterns = [
                rf"{person_lower}.*{user_name}",
                rf"{user_name}.*{person_lower}",
                rf"{person_lower}\s*x\s*{user_name}",
                rf"{user_name}\s*x\s*{person_lower}",
                rf"{person_lower}[-/]{user_name}",
                rf"{user_name}[-/]{person_lower}",
                rf"^{person_lower}\b",  # Starts with person name
            ]
            for pattern in patterns:
                if re.search(pattern, filename_lower):
                    return (
                        f"{work_path}/Meetings",
                        ["meeting", "work", "1-1"],
                        f"1-1 meeting with {person}"
                    )

        # 3. Check content-based classification rules
        for rule in EFFECTIVE_CLASSIFICATION_RULES:
            for pattern in rule["patterns"]:
                if re.search(pattern, content_lower, re.IGNORECASE):
                    rationale = f"Content matched '{pattern}' for category '{rule['name']}'"
                    return rule["destination"], rule["tags"], rationale

        # 4. Default: Work meetings folder
        return (
            f"{work_path}/Meetings",
            ["meeting", "work"],
            "Default classification - work meeting"
        )

    def extract_people(self, content: str) -> list[str]:
        """Extract people mentions from content."""
        people_found = []
        for person in CURRENT_COLLEAGUES:
            if re.search(rf"\b{person}\b", content, re.IGNORECASE):
                people_found.append(person)
        return list(set(people_found))

    def update_frontmatter(
        self,
        content: str,
        tags: list[str],
        people: list[str]
    ) -> str:
        """
        Update frontmatter with proper LifeOS fields.

        Preserves Granola-specific fields (granola_id, granola_url, created_at, updated_at).
        Adds: created, modified, tags, type, people.
        """
        try:
            post = frontmatter.loads(content)
        except Exception:
            post = frontmatter.Post(content)

        # Extract created date from Granola's created_at field
        if "created_at" in post.metadata:
            created_at = post.metadata["created_at"]
            if isinstance(created_at, str):
                try:
                    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    post.metadata["created"] = dt.strftime("%Y-%m-%d")
                except Exception:
                    pass
            elif isinstance(created_at, datetime):
                post.metadata["created"] = created_at.strftime("%Y-%m-%d")

        # Set modified date
        post.metadata["modified"] = datetime.now().strftime("%Y-%m-%d")

        # Merge tags (preserve existing, add new)
        existing_tags = post.metadata.get("tags", [])
        if isinstance(existing_tags, str):
            existing_tags = [existing_tags]
        merged_tags = list(set(existing_tags + tags))
        post.metadata["tags"] = merged_tags

        # Set type
        post.metadata["type"] = "meeting"

        # Add people
        existing_people = post.metadata.get("people", [])
        if isinstance(existing_people, str):
            existing_people = [existing_people]
        merged_people = list(set(existing_people + people))
        if merged_people:
            post.metadata["people"] = merged_people

        return frontmatter.dumps(post)

    def process_file(self, file_path: str) -> Optional[str]:
        """
        Process a single Granola file.

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

        # Check if file is in Granola folder
        try:
            path.relative_to(self.granola_path)
        except ValueError:
            logger.debug(f"File not in Granola folder, skipping: {file_path}")
            return None

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to read {file_path}: {e}")
            return None

        # Extract granola_id for duplicate detection
        granola_id = None
        try:
            post = frontmatter.loads(content)
            granola_id = post.metadata.get("granola_id")
        except Exception:
            pass

        # If this granola_id already exists elsewhere, delete the duplicates first
        if granola_id:
            deleted = self.delete_duplicates_by_granola_id(granola_id, keep_path=path)
            if deleted > 0:
                logger.info(f"Deleted {deleted} existing duplicate(s) for granola_id {granola_id}")

        # Classify the note
        destination, tags, rationale = self.classify_note(content, path.name)

        # Extract people
        people = self.extract_people(content)

        # Update frontmatter
        updated_content = self.update_frontmatter(content, tags, people)

        # Determine destination path
        dest_folder = self.vault_path / destination
        dest_folder.mkdir(parents=True, exist_ok=True)
        dest_path = dest_folder / path.name

        # Handle filename conflicts (only if different granola_id or no granola_id)
        if dest_path.exists() and dest_path != path:
            # Check if the existing file has the same granola_id
            try:
                existing_content = dest_path.read_text(encoding="utf-8")
                existing_post = frontmatter.loads(existing_content)
                existing_granola_id = existing_post.metadata.get("granola_id")
                if existing_granola_id == granola_id and granola_id is not None:
                    # Same granola_id - this is a duplicate, delete it
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
                # Can't read existing file, just rename
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
                # If deletion fails, we have a duplicate - delete the new file to maintain consistency
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

        Unlike process_file (which only processes files in Granola/), this can
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

        # Check if this is a Granola file (has granola_id in frontmatter)
        granola_id = None
        try:
            post = frontmatter.loads(content)
            granola_id = post.metadata.get("granola_id")
            if not granola_id:
                logger.debug(f"Not a Granola file, skipping: {file_path}")
                return None
        except Exception:
            return None

        # Classify the note
        destination, tags, rationale = self.classify_note(content, path.name)

        # Determine correct destination path
        dest_folder = self.vault_path / destination
        dest_path = dest_folder / path.name

        # Check if already in correct location
        try:
            current_dest = path.parent.relative_to(self.vault_path)
            if str(current_dest) == destination:
                # Already in correct location - but still check for duplicates elsewhere
                deleted = self.delete_duplicates_by_granola_id(granola_id, keep_path=path)
                if deleted > 0:
                    logger.info(f"Deleted {deleted} duplicate(s) for granola_id {granola_id}, kept {path}")
                    return str(path)  # Return path to indicate we did something
                logger.debug(f"File already in correct location: {file_path}")
                return None
        except ValueError:
            pass

        # Delete any existing duplicates with the same granola_id (except current file)
        deleted = self.delete_duplicates_by_granola_id(granola_id, keep_path=path)
        if deleted > 0:
            logger.info(f"Deleted {deleted} existing duplicate(s) for granola_id {granola_id}")

        # Extract people
        people = self.extract_people(content)

        # Update frontmatter
        updated_content = self.update_frontmatter(content, tags, people)

        # Create destination folder if needed
        dest_folder.mkdir(parents=True, exist_ok=True)

        # Handle filename conflicts (only for different granola_ids)
        if dest_path.exists() and dest_path != path:
            # Check if the existing file has the same granola_id
            try:
                existing_content = dest_path.read_text(encoding="utf-8")
                existing_post = frontmatter.loads(existing_content)
                existing_granola_id = existing_post.metadata.get("granola_id")
                if existing_granola_id == granola_id:
                    # Same granola_id - this is a duplicate, delete it
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
                # Can't read existing file, just rename
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
                # If deletion fails, we have a duplicate - delete the new file to maintain consistency
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
        Scan a folder and reclassify any Granola files that are in the wrong location.

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
        Find all duplicate Granola files in the vault (files with the same granola_id).

        Returns:
            Dict mapping granola_id to list of file paths (only for IDs with 2+ files)
        """
        granola_files: dict[str, list[Path]] = {}

        for md_file in self.vault_path.rglob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                post = frontmatter.loads(content)
                granola_id = post.metadata.get("granola_id")
                if granola_id:
                    if granola_id not in granola_files:
                        granola_files[granola_id] = []
                    granola_files[granola_id].append(md_file)
            except Exception:
                continue

        # Return only duplicates (2+ files with same granola_id)
        return {gid: paths for gid, paths in granola_files.items() if len(paths) > 1}

    def deduplicate_all(self) -> dict:
        """
        Find and remove all duplicate Granola files in the vault.

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

        for granola_id, paths in duplicates.items():
            # Classify each file to find the best location
            best_path = None
            best_score = -1

            for path in paths:
                try:
                    content = path.read_text(encoding="utf-8")
                    destination, _, rationale = self.classify_note(content, path.name)

                    # Calculate score: higher is better
                    # Score 2: file is in the correct destination folder
                    # Score 1: file is in a subfolder of the correct destination
                    # Score 0: file is in wrong location
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

            # If no best path determined, keep the first one
            if best_path is None:
                best_path = paths[0]

            # Delete all duplicates except the best one
            detail = {
                "granola_id": granola_id,
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
        Process all existing files in the Granola folder.

        Returns:
            Dict with 'processed', 'failed', 'skipped' counts and 'moves' list
        """
        results = {
            "processed": 0,
            "failed": 0,
            "skipped": 0,
            "moves": []
        }

        if not self.granola_path.exists():
            logger.warning(f"Granola folder does not exist: {self.granola_path}")
            return results

        for md_file in self.granola_path.glob("*.md"):
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

        logger.debug("Running Granola processor cycle")
        try:
            results = self.process_backlog()
            if results["processed"] > 0:
                logger.info(f"Granola cycle: processed {results['processed']} files")
        except Exception as e:
            logger.error(f"Granola processor cycle failed: {e}")
            # Record failure for nightly batch report
            try:
                from api.services.notifications import record_failure
                record_failure("Granola processor", str(e))
            except Exception as notify_err:
                logger.error(f"Failed to record Granola failure: {notify_err}")

        # Schedule next run
        if self._running:
            self._timer = threading.Timer(self.interval_seconds, self._run_cycle)
            self._timer.daemon = True
            self._timer.start()

    def start(self) -> None:
        """Start the processor (runs every interval_seconds)."""
        with self._lock:
            if self._running:
                logger.debug("Granola processor already running")
                return

            if not self.granola_path.exists():
                logger.warning(f"Granola folder does not exist: {self.granola_path}")
                return

            self._running = True

            # Run immediately on start
            logger.info(f"Starting Granola processor (interval: {self.interval_seconds}s)")
            self._run_cycle()

    # Alias for backward compatibility
    def start_watching(self) -> None:
        """Alias for start() for backward compatibility."""
        self.start()

    def stop(self) -> None:
        """Stop the processor."""
        with self._lock:
            self._running = False
            if self._timer:
                self._timer.cancel()
                self._timer = None
            logger.info("Stopped Granola processor")

    @property
    def is_running(self) -> bool:
        """Check if processor is running."""
        return self._running

    # Alias for backward compatibility
    @property
    def is_watching(self) -> bool:
        """Alias for is_running for backward compatibility."""
        return self._running


# Singleton instance
_processor_instance: Optional[GranolaProcessor] = None


def get_granola_processor(vault_path: str) -> GranolaProcessor:
    """Get or create the Granola processor singleton."""
    global _processor_instance
    if _processor_instance is None:
        _processor_instance = GranolaProcessor(vault_path)
    return _processor_instance
