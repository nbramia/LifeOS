"""
Google Drive integration service for LifeOS.

Provides search and retrieval of Drive files.
Supports Google Docs, Sheets, and other file types.
"""
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

from googleapiclient.discovery import build

from api.services.google_auth import get_google_auth, GoogleAccount

logger = logging.getLogger(__name__)


# MIME types for Google-native formats
MIME_GOOGLE_DOC = "application/vnd.google-apps.document"
MIME_GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
MIME_GOOGLE_SLIDES = "application/vnd.google-apps.presentation"
MIME_GOOGLE_FOLDER = "application/vnd.google-apps.folder"


def get_drive_link(file_id: str, mime_type: str) -> str:
    """
    Generate the web link for a Drive file.

    Args:
        file_id: Drive file ID
        mime_type: File MIME type

    Returns:
        Web URL to access the file
    """
    if mime_type == MIME_GOOGLE_DOC:
        return f"https://docs.google.com/document/d/{file_id}"
    elif mime_type == MIME_GOOGLE_SHEET:
        return f"https://docs.google.com/spreadsheets/d/{file_id}"
    elif mime_type == MIME_GOOGLE_SLIDES:
        return f"https://docs.google.com/presentation/d/{file_id}"
    else:
        return f"https://drive.google.com/file/d/{file_id}"


@dataclass
class DriveFile:
    """Represents a Google Drive file."""
    file_id: str
    name: str
    mime_type: str
    modified_time: datetime
    source_account: str
    web_link: Optional[str] = None
    content: Optional[str] = None
    size: Optional[int] = None
    parent_folder: Optional[str] = None

    @property
    def is_google_doc(self) -> bool:
        return self.mime_type == MIME_GOOGLE_DOC

    @property
    def is_google_sheet(self) -> bool:
        return self.mime_type == MIME_GOOGLE_SHEET

    @property
    def is_folder(self) -> bool:
        return self.mime_type == MIME_GOOGLE_FOLDER

    def to_dict(self) -> dict:
        """Convert to dict for API response."""
        return {
            "file_id": self.file_id,
            "name": self.name,
            "mime_type": self.mime_type,
            "modified_time": self.modified_time.isoformat(),
            "web_link": self.web_link or get_drive_link(self.file_id, self.mime_type),
            "content": self.content,
            "size": self.size,
            "source": "google_drive",
            "source_account": self.source_account,
        }


class DriveService:
    """
    Google Drive service for searching and retrieving files.
    """

    def __init__(self, account_type: GoogleAccount = GoogleAccount.PERSONAL):
        """
        Initialize Drive service.

        Args:
            account_type: Which Google account to use
        """
        self.account_type = account_type
        self._service = None

    @property
    def service(self):
        """Get or create Drive API service."""
        if self._service is None:
            auth = get_google_auth(self.account_type)
            credentials = auth.get_credentials()
            self._service = build("drive", "v3", credentials=credentials)
        return self._service

    def search(
        self,
        name: Optional[str] = None,
        full_text: Optional[str] = None,
        mime_type: Optional[str] = None,
        folder_id: Optional[str] = None,
        max_results: int = 20,
    ) -> list[DriveFile]:
        """
        Search Drive files.

        Args:
            name: Search by file name (partial match)
            full_text: Search in file content
            mime_type: Filter by MIME type
            folder_id: Search within specific folder
            max_results: Maximum files to return

        Returns:
            List of DriveFile objects
        """
        query_parts = []

        # Build query
        if name:
            query_parts.append(f"name contains '{name}'")

        if full_text:
            query_parts.append(f"fullText contains '{full_text}'")

        if mime_type:
            query_parts.append(f"mimeType = '{mime_type}'")

        if folder_id:
            query_parts.append(f"'{folder_id}' in parents")

        # Exclude trash
        query_parts.append("trashed = false")

        # Exclude folders by default for search
        if not mime_type:
            query_parts.append(f"mimeType != '{MIME_GOOGLE_FOLDER}'")

        query = " and ".join(query_parts)

        try:
            result = self.service.files().list(
                q=query,
                pageSize=max_results,
                fields="files(id, name, mimeType, modifiedTime, webViewLink, size, parents)",
                orderBy="modifiedTime desc",
            ).execute()

            files = result.get("files", [])
            return [self._parse_file(f) for f in files]

        except Exception as e:
            logger.error(f"Failed to search Drive: {e}")
            return []

    def get_file(self, file_id: str) -> Optional[DriveFile]:
        """
        Get file metadata by ID.

        Args:
            file_id: Drive file ID

        Returns:
            DriveFile or None if not found
        """
        try:
            result = self.service.files().get(
                fileId=file_id,
                fields="id, name, mimeType, modifiedTime, webViewLink, size, parents",
            ).execute()

            return self._parse_file(result)

        except Exception as e:
            logger.error(f"Failed to get file {file_id}: {e}")
            return None

    def get_file_content(self, file_id: str, mime_type: str) -> Optional[str]:
        """
        Get file content as text.

        For Google Docs/Sheets, exports as plain text/CSV.
        For other files, downloads if text-based.

        Args:
            file_id: Drive file ID
            mime_type: File MIME type

        Returns:
            File content as text, or None if not supported
        """
        try:
            if mime_type == MIME_GOOGLE_DOC:
                # Export Google Doc as plain text
                content = self.service.files().export(
                    fileId=file_id,
                    mimeType="text/plain"
                ).execute()
                return content.decode("utf-8") if isinstance(content, bytes) else content

            elif mime_type == MIME_GOOGLE_SHEET:
                # Export Google Sheet as CSV
                content = self.service.files().export(
                    fileId=file_id,
                    mimeType="text/csv"
                ).execute()
                return content.decode("utf-8") if isinstance(content, bytes) else content

            elif mime_type == MIME_GOOGLE_SLIDES:
                # Export slides as plain text
                content = self.service.files().export(
                    fileId=file_id,
                    mimeType="text/plain"
                ).execute()
                return content.decode("utf-8") if isinstance(content, bytes) else content

            elif mime_type.startswith("text/"):
                # Download text files directly
                content = self.service.files().get_media(fileId=file_id).execute()
                return content.decode("utf-8") if isinstance(content, bytes) else content

            else:
                logger.warning(f"Cannot extract text from {mime_type}")
                return None

        except Exception as e:
            logger.error(f"Failed to get file content {file_id}: {e}")
            return None

    def export_doc_as_html(self, file_id: str) -> str:
        """
        Export a Google Doc as HTML.

        Preserves formatting including headings, bold, lists, links, etc.

        Args:
            file_id: Google Doc file ID

        Returns:
            HTML content as string

        Raises:
            Exception: If export fails
        """
        result = self.service.files().export(
            fileId=file_id,
            mimeType="text/html"
        ).execute()
        return result.decode("utf-8") if isinstance(result, bytes) else result

    def _parse_file(self, file_data: dict) -> DriveFile:
        """
        Parse raw API file data into DriveFile.

        Args:
            file_data: Raw file dict from API

        Returns:
            DriveFile object
        """
        modified_str = file_data.get("modifiedTime", "")
        try:
            # Parse ISO format with Z suffix
            modified_time = datetime.fromisoformat(modified_str.replace("Z", "+00:00"))
        except Exception:
            modified_time = datetime.now(timezone.utc)

        parents = file_data.get("parents", [])
        parent_folder = parents[0] if parents else None

        return DriveFile(
            file_id=file_data.get("id", ""),
            name=file_data.get("name", ""),
            mime_type=file_data.get("mimeType", ""),
            modified_time=modified_time,
            web_link=file_data.get("webViewLink"),
            size=file_data.get("size"),
            parent_folder=parent_folder,
            source_account=self.account_type.value,
        )


# Singleton services per account
_drive_services: dict[GoogleAccount, DriveService] = {}


def get_drive_service(account_type: GoogleAccount = GoogleAccount.PERSONAL) -> DriveService:
    """Get or create Drive service for an account."""
    if account_type not in _drive_services:
        _drive_services[account_type] = DriveService(account_type)
    return _drive_services[account_type]
