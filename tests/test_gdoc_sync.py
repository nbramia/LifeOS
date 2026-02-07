"""
Tests for Google Docs sync service.
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open
from datetime import datetime, timezone
import tempfile

# Mark all tests as unit tests
pytestmark = pytest.mark.unit


class TestHtmlToMarkdown:
    """Test HTML to Markdown conversion."""

    def test_converts_headings(self):
        """Should convert HTML headings to Markdown ATX style."""
        from api.services.gdoc_sync import GDocSyncService

        service = GDocSyncService.__new__(GDocSyncService)
        service.mappings = []

        html = "<h1>Title</h1><p>Content</p>"
        md = service._html_to_markdown(html)

        assert "# Title" in md
        assert "Content" in md

    def test_converts_bold(self):
        """Should convert bold text."""
        from api.services.gdoc_sync import GDocSyncService

        service = GDocSyncService.__new__(GDocSyncService)
        service.mappings = []

        html = "<p>This is <b>bold</b> text</p>"
        md = service._html_to_markdown(html)

        assert "**bold**" in md

    def test_converts_lists(self):
        """Should convert lists with dash bullets."""
        from api.services.gdoc_sync import GDocSyncService

        service = GDocSyncService.__new__(GDocSyncService)
        service.mappings = []

        html = "<ul><li>Item 1</li><li>Item 2</li></ul>"
        md = service._html_to_markdown(html)

        assert "- Item 1" in md
        assert "- Item 2" in md

    def test_converts_links(self):
        """Should convert links to Markdown format."""
        from api.services.gdoc_sync import GDocSyncService

        service = GDocSyncService.__new__(GDocSyncService)
        service.mappings = []

        html = '<p>Click <a href="https://example.com">here</a></p>'
        md = service._html_to_markdown(html)

        assert "[here](https://example.com)" in md

    def test_strips_style_tags(self):
        """Should strip style and script tags."""
        from api.services.gdoc_sync import GDocSyncService

        service = GDocSyncService.__new__(GDocSyncService)
        service.mappings = []

        html = "<style>body{color:red}</style><p>Content</p><script>alert(1)</script>"
        md = service._html_to_markdown(html)

        assert "style" not in md.lower()
        assert "script" not in md.lower()
        assert "Content" in md

    def test_removes_excessive_whitespace(self):
        """Should remove consecutive blank lines."""
        from api.services.gdoc_sync import GDocSyncService

        service = GDocSyncService.__new__(GDocSyncService)
        service.mappings = []

        html = "<p>Para 1</p><br><br><br><p>Para 2</p>"
        md = service._html_to_markdown(html)

        # Should not have more than 2 consecutive newlines
        assert "\n\n\n" not in md


class TestBuildFileContent:
    """Test file content building with frontmatter."""

    def test_includes_frontmatter(self):
        """Should include frontmatter with sync metadata."""
        from api.services.gdoc_sync import GDocSyncService

        service = GDocSyncService.__new__(GDocSyncService)
        service.mappings = []

        content = service._build_file_content(
            "# Test",
            doc_id="abc123",
            doc_url="https://docs.google.com/document/d/abc123/edit",
        )

        assert "---" in content
        assert "gdoc_sync: true" in content
        assert 'gdoc_id: "abc123"' in content

    def test_includes_warning_callout(self):
        """Should include Obsidian warning callout."""
        from api.services.gdoc_sync import GDocSyncService

        service = GDocSyncService.__new__(GDocSyncService)
        service.mappings = []

        content = service._build_file_content(
            "# Test",
            doc_id="abc123",
            doc_url="https://docs.google.com/document/d/abc123/edit",
        )

        assert "[!warning]" in content
        assert "Do not edit locally" in content
        assert "Edit in Google Docs" in content

    def test_includes_edit_link(self):
        """Should include link to edit in Google Docs."""
        from api.services.gdoc_sync import GDocSyncService

        service = GDocSyncService.__new__(GDocSyncService)
        service.mappings = []

        doc_url = "https://docs.google.com/document/d/abc123/edit"
        content = service._build_file_content(
            "# Test",
            doc_id="abc123",
            doc_url=doc_url,
        )

        assert doc_url in content

    def test_includes_last_synced_timestamp(self):
        """Should include ISO timestamp for last sync."""
        from api.services.gdoc_sync import GDocSyncService

        service = GDocSyncService.__new__(GDocSyncService)
        service.mappings = []

        content = service._build_file_content(
            "# Test",
            doc_id="abc123",
            doc_url="https://docs.google.com/document/d/abc123/edit",
        )

        assert "last_synced:" in content
        # Should have ISO format timestamp
        assert "202" in content  # Year starts with 202x


class TestConfigLoading:
    """Test YAML config loading."""

    def test_loads_valid_config(self):
        """Should load documents from valid YAML config."""
        from api.services.gdoc_sync import GDocSyncService, CONFIG_PATH

        yaml_content = """
sync_enabled: true
documents:
  - doc_id: "doc1"
    vault_path: "Test/Doc1.md"
    account: "personal"
  - doc_id: "doc2"
    vault_path: "Work/Doc2.md"
    account: "work"
"""
        with patch("builtins.open", mock_open(read_data=yaml_content)):
            with patch.object(Path, "exists", return_value=True):
                service = GDocSyncService.__new__(GDocSyncService)
                service.mappings = []
                service._load_config()

        assert len(service.mappings) == 2
        assert service.mappings[0].doc_id == "doc1"
        assert service.mappings[0].vault_path == "Test/Doc1.md"
        assert service.mappings[1].account == "work"

    def test_skips_when_disabled(self):
        """Should not load documents when sync_enabled is false."""
        from api.services.gdoc_sync import GDocSyncService

        yaml_content = """
sync_enabled: false
documents:
  - doc_id: "doc1"
    vault_path: "Test/Doc1.md"
"""
        with patch("builtins.open", mock_open(read_data=yaml_content)):
            with patch.object(Path, "exists", return_value=True):
                service = GDocSyncService.__new__(GDocSyncService)
                service.mappings = []
                service._load_config()

        assert len(service.mappings) == 0

    def test_handles_missing_config(self):
        """Should handle missing config file gracefully."""
        from api.services.gdoc_sync import GDocSyncService

        with patch.object(Path, "exists", return_value=False):
            service = GDocSyncService.__new__(GDocSyncService)
            service.mappings = []
            service._load_config()

        assert len(service.mappings) == 0

    def test_skips_invalid_entries(self):
        """Should skip entries missing required fields."""
        from api.services.gdoc_sync import GDocSyncService

        yaml_content = """
sync_enabled: true
documents:
  - doc_id: "valid"
    vault_path: "Test/Valid.md"
  - vault_path: "Missing/DocId.md"
  - doc_id: "missing_path"
"""
        with patch("builtins.open", mock_open(read_data=yaml_content)):
            with patch.object(Path, "exists", return_value=True):
                service = GDocSyncService.__new__(GDocSyncService)
                service.mappings = []
                service._load_config()

        assert len(service.mappings) == 1
        assert service.mappings[0].doc_id == "valid"


class TestSyncDoc:
    """Test document sync functionality."""

    def test_sync_doc_creates_file(self):
        """Should create markdown file from Google Doc."""
        from api.services.gdoc_sync import GDocSyncService, DocMapping

        with tempfile.TemporaryDirectory() as tmpdir:
            service = GDocSyncService.__new__(GDocSyncService)
            service.vault_path = Path(tmpdir)
            service.mappings = []

            mapping = DocMapping(
                doc_id="test123",
                vault_path="Test/Synced.md",
                account="personal",
            )

            mock_drive = MagicMock()
            mock_drive.export_doc_as_html.return_value = "<h1>Test Doc</h1><p>Content here</p>"

            with patch("api.services.gdoc_sync.get_drive_service", return_value=mock_drive):
                result = service.sync_doc(mapping)

            assert result.exists()
            content = result.read_text()
            assert "gdoc_sync: true" in content
            assert "# Test Doc" in content
            assert "Content here" in content

    def test_sync_all_returns_stats(self):
        """Should return stats from sync operation."""
        from api.services.gdoc_sync import GDocSyncService, DocMapping

        with tempfile.TemporaryDirectory() as tmpdir:
            service = GDocSyncService.__new__(GDocSyncService)
            service.vault_path = Path(tmpdir)
            service.mappings = [
                DocMapping("doc1", "Test/Doc1.md", "personal"),
                DocMapping("doc2", "Test/Doc2.md", "personal"),
            ]

            mock_drive = MagicMock()
            mock_drive.export_doc_as_html.return_value = "<p>Test</p>"

            with patch("api.services.gdoc_sync.get_drive_service", return_value=mock_drive):
                stats = service.sync_all()

            assert stats["synced"] == 2
            assert stats["failed"] == 0
            assert len(stats["documents"]) == 2


class TestDriveExport:
    """Test Drive service HTML export."""

    def test_export_doc_as_html(self):
        """Should export Google Doc as HTML."""
        from api.services.drive import DriveService, GoogleAccount

        service = DriveService.__new__(DriveService)
        service.account_type = GoogleAccount.PERSONAL
        service._service = MagicMock()
        service._service.files.return_value.export.return_value.execute.return_value = b"<h1>Test</h1>"

        result = service.export_doc_as_html("test_doc_id")

        assert result == "<h1>Test</h1>"
        service._service.files.return_value.export.assert_called_once_with(
            fileId="test_doc_id",
            mimeType="text/html"
        )
