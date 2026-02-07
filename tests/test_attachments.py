"""
Tests for multi-modal attachment handling.

Unit tests for attachment validation and message content building.
"""
import base64
import pytest
from pydantic import ValidationError

from api.routes.chat import (
    Attachment,
    AskStreamRequest,
    ALLOWED_MEDIA_TYPES,
    MAX_ATTACHMENTS,
    MAX_TOTAL_SIZE,
)
from api.services.synthesizer import build_message_content

# Mark all tests in this module as unit tests
pytestmark = pytest.mark.unit


class TestAttachmentModel:
    """Tests for the Attachment Pydantic model."""

    def test_valid_image_attachment(self):
        """Test creating a valid image attachment."""
        # Small valid base64 data (a tiny PNG)
        data = base64.b64encode(b"fake png data").decode()
        att = Attachment(
            filename="test.png",
            media_type="image/png",
            data=data
        )
        assert att.filename == "test.png"
        assert att.media_type == "image/png"

    def test_valid_pdf_attachment(self):
        """Test creating a valid PDF attachment."""
        data = base64.b64encode(b"fake pdf data").decode()
        att = Attachment(
            filename="document.pdf",
            media_type="application/pdf",
            data=data
        )
        assert att.filename == "document.pdf"
        assert att.media_type == "application/pdf"

    def test_valid_text_attachment(self):
        """Test creating a valid text attachment."""
        data = base64.b64encode(b"Hello, world!").decode()
        att = Attachment(
            filename="notes.txt",
            media_type="text/plain",
            data=data
        )
        assert att.filename == "notes.txt"

    def test_invalid_media_type_rejected(self):
        """Test that unsupported media types are rejected."""
        data = base64.b64encode(b"some data").decode()
        with pytest.raises(ValidationError) as exc_info:
            Attachment(
                filename="virus.exe",
                media_type="application/x-msdownload",
                data=data
            )
        assert "Unsupported file type" in str(exc_info.value)

    def test_get_size_bytes(self):
        """Test size calculation from base64 data."""
        original = b"Hello, this is test data!"
        data = base64.b64encode(original).decode()
        att = Attachment(
            filename="test.txt",
            media_type="text/plain",
            data=data
        )
        # Size should be approximately equal to original (base64 adds ~33%)
        calculated_size = att.get_size_bytes()
        # Allow some margin for base64 padding
        assert abs(calculated_size - len(original)) <= 3

    def test_size_validation_rejects_oversized(self):
        """Test that oversized attachments are rejected."""
        # Create data larger than 1MB limit for text files
        large_data = base64.b64encode(b"x" * (2 * 1024 * 1024)).decode()
        att = Attachment(
            filename="huge.txt",
            media_type="text/plain",
            data=large_data
        )
        with pytest.raises(ValueError) as exc_info:
            att.validate_size()
        assert "exceeds limit" in str(exc_info.value)


class TestAskStreamRequest:
    """Tests for the AskStreamRequest with attachments."""

    def test_request_without_attachments(self):
        """Test creating request without attachments."""
        req = AskStreamRequest(question="What is this?")
        assert req.question == "What is this?"
        assert req.attachments is None

    def test_request_with_valid_attachments(self):
        """Test creating request with valid attachments."""
        data = base64.b64encode(b"test data").decode()
        req = AskStreamRequest(
            question="What is in this image?",
            attachments=[
                Attachment(filename="test.png", media_type="image/png", data=data)
            ]
        )
        assert len(req.attachments) == 1

    def test_max_attachments_enforced(self):
        """Test that max attachment count is enforced."""
        data = base64.b64encode(b"test").decode()
        attachments = [
            Attachment(filename=f"file{i}.png", media_type="image/png", data=data)
            for i in range(MAX_ATTACHMENTS + 1)
        ]
        with pytest.raises(ValidationError) as exc_info:
            AskStreamRequest(question="Too many files", attachments=attachments)
        assert "Maximum" in str(exc_info.value)

    def test_total_size_limit_enforced(self):
        """Test that total attachment size limit is enforced."""
        # Create attachments that together exceed 20MB
        large_data = base64.b64encode(b"x" * (5 * 1024 * 1024)).decode()  # ~5MB each
        attachments = [
            Attachment(filename=f"file{i}.png", media_type="image/png", data=large_data)
            for i in range(5)  # 5 x 5MB = 25MB > 20MB limit
        ]
        with pytest.raises(ValidationError) as exc_info:
            AskStreamRequest(question="Files too large", attachments=attachments)
        assert "exceeds limit" in str(exc_info.value)


class TestBuildMessageContent:
    """Tests for the build_message_content function."""

    def test_text_only_returns_string(self):
        """Test that text-only prompts return a simple string."""
        result = build_message_content("Hello, Claude!")
        assert isinstance(result, str)
        assert result == "Hello, Claude!"

    def test_with_image_returns_list(self):
        """Test that prompts with images return a list of content blocks."""
        data = base64.b64encode(b"fake image data").decode()
        attachments = [{
            "filename": "screenshot.png",
            "media_type": "image/png",
            "data": data
        }]
        result = build_message_content("What is in this image?", attachments)

        assert isinstance(result, list)
        assert len(result) == 2  # image + text

        # First should be image
        assert result[0]["type"] == "image"
        assert result[0]["source"]["type"] == "base64"
        assert result[0]["source"]["media_type"] == "image/png"

        # Last should be text
        assert result[-1]["type"] == "text"
        assert "What is in this image?" in result[-1]["text"]

    def test_with_pdf_returns_document_block(self):
        """Test that PDFs are converted to document blocks."""
        data = base64.b64encode(b"fake pdf data").decode()
        attachments = [{
            "filename": "document.pdf",
            "media_type": "application/pdf",
            "data": data
        }]
        result = build_message_content("Summarize this PDF", attachments)

        assert isinstance(result, list)
        assert result[0]["type"] == "document"
        assert result[0]["source"]["media_type"] == "application/pdf"

    def test_text_file_appended_to_prompt(self):
        """Test that text files are decoded and appended to prompt."""
        text_content = "This is the content of my file."
        data = base64.b64encode(text_content.encode()).decode()
        attachments = [{
            "filename": "notes.txt",
            "media_type": "text/plain",
            "data": data
        }]
        result = build_message_content("What does this file say?", attachments)

        assert isinstance(result, list)
        # Only text block (text files are appended to prompt, not separate)
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert "notes.txt" in result[0]["text"]
        assert text_content in result[0]["text"]

    def test_mixed_attachments(self):
        """Test handling of mixed attachment types."""
        image_data = base64.b64encode(b"fake image").decode()
        text_content = "Some text content"
        text_data = base64.b64encode(text_content.encode()).decode()

        attachments = [
            {"filename": "photo.png", "media_type": "image/png", "data": image_data},
            {"filename": "notes.txt", "media_type": "text/plain", "data": text_data},
        ]
        result = build_message_content("Describe this", attachments)

        assert isinstance(result, list)
        # Should have: image block + text block (with notes.txt content appended)
        assert len(result) == 2
        assert result[0]["type"] == "image"
        assert result[1]["type"] == "text"
        assert text_content in result[1]["text"]

    def test_multiple_images(self):
        """Test handling of multiple image attachments."""
        data1 = base64.b64encode(b"image 1").decode()
        data2 = base64.b64encode(b"image 2").decode()

        attachments = [
            {"filename": "img1.png", "media_type": "image/png", "data": data1},
            {"filename": "img2.jpg", "media_type": "image/jpeg", "data": data2},
        ]
        result = build_message_content("Compare these images", attachments)

        assert isinstance(result, list)
        assert len(result) == 3  # 2 images + text
        assert result[0]["type"] == "image"
        assert result[1]["type"] == "image"
        assert result[2]["type"] == "text"


class TestAllowedMediaTypes:
    """Tests for media type configuration."""

    def test_common_image_types_allowed(self):
        """Test that common image types are in the allowed list."""
        assert "image/png" in ALLOWED_MEDIA_TYPES
        assert "image/jpeg" in ALLOWED_MEDIA_TYPES
        assert "image/gif" in ALLOWED_MEDIA_TYPES
        assert "image/webp" in ALLOWED_MEDIA_TYPES

    def test_pdf_allowed(self):
        """Test that PDF is in the allowed list."""
        assert "application/pdf" in ALLOWED_MEDIA_TYPES

    def test_text_types_allowed(self):
        """Test that text types are in the allowed list."""
        assert "text/plain" in ALLOWED_MEDIA_TYPES
        assert "text/markdown" in ALLOWED_MEDIA_TYPES
        assert "text/csv" in ALLOWED_MEDIA_TYPES
        assert "application/json" in ALLOWED_MEDIA_TYPES

    def test_image_size_limit(self):
        """Test that image size limit is 5MB."""
        assert ALLOWED_MEDIA_TYPES["image/png"] == 5 * 1024 * 1024

    def test_pdf_size_limit(self):
        """Test that PDF size limit is 10MB."""
        assert ALLOWED_MEDIA_TYPES["application/pdf"] == 10 * 1024 * 1024

    def test_text_size_limit(self):
        """Test that text file size limit is 1MB."""
        assert ALLOWED_MEDIA_TYPES["text/plain"] == 1 * 1024 * 1024
