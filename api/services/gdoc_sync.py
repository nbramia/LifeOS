"""
Google Docs to Obsidian sync service.

Provides one-way sync from Google Docs to Markdown files in the vault.
Exports as HTML and converts to Markdown, preserving formatting.
"""
import logging
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from bs4 import BeautifulSoup
from markdownify import markdownify as md

from api.services.drive import get_drive_service, GoogleAccount
from config.settings import settings

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "gdoc_sync.yaml"


@dataclass
class DocMapping:
    """Mapping from Google Doc to vault file."""
    doc_id: str
    vault_path: str
    account: str  # "personal" or "work"


class GDocSyncService:
    """
    Service for syncing Google Docs to Obsidian vault.

    One-way sync: Google Doc content always overwrites local file.
    Files include frontmatter with sync metadata and a warning callout.
    """

    def __init__(self, vault_path: Optional[Path] = None):
        """
        Initialize the sync service.

        Args:
            vault_path: Path to Obsidian vault (defaults to settings.vault_path)
        """
        self.vault_path = vault_path or settings.vault_path
        self.mappings: list[DocMapping] = []
        self._load_config()

    def _load_config(self):
        """Load sync configuration from YAML file."""
        if not CONFIG_PATH.exists():
            logger.info(f"GDoc sync config not found at {CONFIG_PATH}, skipping")
            return

        try:
            with open(CONFIG_PATH) as f:
                config = yaml.safe_load(f)

            if not config:
                logger.info("GDoc sync config is empty")
                return

            if not config.get("sync_enabled", True):
                logger.info("GDoc sync is disabled in config")
                return

            for doc in config.get("documents", []):
                if not doc.get("doc_id") or not doc.get("vault_path"):
                    logger.warning(f"Skipping invalid doc mapping: {doc}")
                    continue

                self.mappings.append(DocMapping(
                    doc_id=doc["doc_id"],
                    vault_path=doc["vault_path"],
                    account=doc.get("account", "personal"),
                ))

            logger.info(f"Loaded {len(self.mappings)} GDoc sync mappings")

        except Exception as e:
            logger.error(f"Failed to load GDoc sync config: {e}")

    def sync_all(self) -> dict:
        """
        Sync all configured documents.

        Returns:
            Stats dict with counts: synced, failed, skipped
        """
        stats = {
            "synced": 0,
            "failed": 0,
            "skipped": 0,
            "documents": [],
        }

        if not self.mappings:
            logger.info("No documents configured for GDoc sync")
            return stats

        for mapping in self.mappings:
            try:
                file_path = self.sync_doc(mapping)
                stats["synced"] += 1
                stats["documents"].append({
                    "doc_id": mapping.doc_id,
                    "vault_path": mapping.vault_path,
                    "status": "success",
                })
                logger.info(f"Synced {mapping.doc_id} -> {mapping.vault_path}")

            except Exception as e:
                stats["failed"] += 1
                stats["documents"].append({
                    "doc_id": mapping.doc_id,
                    "vault_path": mapping.vault_path,
                    "status": "failed",
                    "error": str(e),
                })
                logger.error(f"Failed to sync {mapping.doc_id}: {e}")

        logger.info(f"GDoc sync complete: {stats['synced']} synced, {stats['failed']} failed")
        return stats

    def sync_doc(self, mapping: DocMapping) -> Path:
        """
        Sync a single Google Doc to its vault file.

        Args:
            mapping: Document mapping configuration

        Returns:
            Path to the synced file

        Raises:
            Exception: If sync fails
        """
        # Get Drive service for appropriate account
        account = GoogleAccount.WORK if mapping.account == "work" else GoogleAccount.PERSONAL
        drive = get_drive_service(account)

        # Export as HTML
        html_content = drive.export_doc_as_html(mapping.doc_id)

        # Convert to Markdown
        markdown_content = self._html_to_markdown(html_content)

        # Build full file with frontmatter and warning
        doc_url = f"https://docs.google.com/document/d/{mapping.doc_id}/edit"
        full_content = self._build_file_content(
            markdown_content,
            doc_id=mapping.doc_id,
            doc_url=doc_url,
        )

        # Write to vault
        file_path = Path(self.vault_path) / mapping.vault_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(full_content, encoding="utf-8")

        return file_path

    def _html_to_markdown(self, html: str) -> str:
        """
        Convert HTML to Markdown.

        Args:
            html: HTML content from Google Docs

        Returns:
            Markdown content
        """
        # Pre-process HTML with BeautifulSoup to remove style/script tags
        # Google Docs exports include extensive CSS that markdownify doesn't fully strip
        soup = BeautifulSoup(html, "html.parser")

        # Remove style, script, head, and meta tags
        for tag in soup.find_all(["style", "script", "head", "meta"]):
            tag.decompose()

        # Convert back to string for markdownify
        clean_html = str(soup)

        # markdownify handles the conversion
        # - heading_style="ATX" uses # style headings
        # - bullets="-" uses - for unordered lists
        markdown = md(
            clean_html,
            heading_style="ATX",
            bullets="-",
        )

        # Clean up excessive whitespace
        lines = markdown.split("\n")
        cleaned_lines = []
        prev_blank = False

        for line in lines:
            is_blank = not line.strip()
            # Skip consecutive blank lines
            if is_blank and prev_blank:
                continue
            cleaned_lines.append(line)
            prev_blank = is_blank

        return "\n".join(cleaned_lines).strip()

    def _build_file_content(
        self,
        content: str,
        doc_id: str,
        doc_url: str,
    ) -> str:
        """
        Build full file with frontmatter and warning callout.

        Args:
            content: Markdown content
            doc_id: Google Doc ID
            doc_url: URL to edit the doc

        Returns:
            Complete file content with frontmatter and warning
        """
        now = datetime.now(timezone.utc).isoformat()

        frontmatter = f"""---
gdoc_sync: true
gdoc_id: "{doc_id}"
gdoc_url: "{doc_url}"
last_synced: "{now}"
---"""

        warning = f"""> [!warning] Auto-Synced Document
> This file is automatically synced from Google Docs. **Do not edit locally.**
> Make changes here: [Edit in Google Docs]({doc_url})"""

        return f"{frontmatter}\n\n{warning}\n\n{content}\n"


# Singleton instance
_gdoc_sync_service: Optional[GDocSyncService] = None


def get_gdoc_sync_service() -> GDocSyncService:
    """
    Get or create the GDoc sync service singleton.

    Returns:
        GDocSyncService instance
    """
    global _gdoc_sync_service
    if _gdoc_sync_service is None:
        _gdoc_sync_service = GDocSyncService()
    return _gdoc_sync_service


def sync_gdocs() -> dict:
    """
    Convenience function for nightly sync.

    Returns:
        Stats dict from sync operation
    """
    service = get_gdoc_sync_service()
    return service.sync_all()
