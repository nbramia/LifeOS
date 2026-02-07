"""
Tests for api/services/chunker.py

Tests markdown parsing and document chunking strategies.
"""
import pytest
from pathlib import Path
from unittest.mock import patch

from api.services.chunker import (
    count_tokens,
    extract_frontmatter,
    parse_markdown,
    chunk_by_headers,
    chunk_by_tokens,
    chunk_document,
    generate_chunk_context,
    add_context_to_chunks,
    _infer_topic,
)


# =============================================================================
# count_tokens Tests
# =============================================================================

@pytest.mark.unit
class TestCountTokens:
    """Tests for count_tokens function."""

    def test_count_tokens_basic(self):
        """Test counting tokens for basic text."""
        result = count_tokens("Hello world")
        assert result >= 2  # At least 2 tokens

    def test_count_tokens_empty(self):
        """Test counting tokens for empty string."""
        result = count_tokens("")
        assert result == 0

    def test_count_tokens_longer_text(self):
        """Test counting tokens for longer text."""
        text = "This is a longer piece of text with more tokens."
        result = count_tokens(text)
        # Should be roughly 10-15 tokens
        assert result >= 8
        assert result <= 20

    def test_count_tokens_consistent(self):
        """Test that token counting is consistent."""
        text = "Same text multiple times"
        result1 = count_tokens(text)
        result2 = count_tokens(text)
        assert result1 == result2


# =============================================================================
# extract_frontmatter Tests
# =============================================================================

@pytest.mark.unit
class TestExtractFrontmatter:
    """Tests for extract_frontmatter function."""

    def test_extract_with_frontmatter(self):
        """Test extracting valid frontmatter."""
        content = """---
title: Test Document
tags:
  - test
  - example
---

# Main Content

Some text here.
"""
        metadata, body = extract_frontmatter(content)

        assert metadata["title"] == "Test Document"
        assert "test" in metadata["tags"]
        assert "# Main Content" in body
        assert "---" not in body

    def test_extract_no_frontmatter(self):
        """Test content with no frontmatter."""
        content = "# Just Content\n\nNo frontmatter here."
        metadata, body = extract_frontmatter(content)

        assert metadata == {}
        assert "# Just Content" in body

    def test_extract_empty_frontmatter(self):
        """Test content with empty frontmatter."""
        content = "---\n---\n\n# Content"
        metadata, body = extract_frontmatter(content)

        assert metadata == {}
        assert "# Content" in body

    def test_extract_granola_frontmatter(self):
        """Test extracting Granola-style frontmatter."""
        content = """---
granola_id: abc123
people:
  - John Smith
  - Jane Doe
modified_date: 2026-01-30
---

## Meeting Notes
"""
        metadata, body = extract_frontmatter(content)

        assert metadata["granola_id"] == "abc123"
        assert "John Smith" in metadata["people"]
        assert "## Meeting Notes" in body

    def test_extract_invalid_yaml(self):
        """Test handling of invalid YAML frontmatter."""
        content = """---
invalid: yaml: content:
---

Content here
"""
        metadata, body = extract_frontmatter(content)
        # Should handle gracefully - either parse or return empty
        assert isinstance(metadata, dict)


# =============================================================================
# parse_markdown Tests
# =============================================================================

@pytest.mark.unit
class TestParseMarkdown:
    """Tests for parse_markdown function."""

    def test_parse_with_headers(self):
        """Test parsing markdown with headers."""
        content = """# Main Title

Some intro text.

## Section One

Content for section one.

## Section Two

Content for section two.
"""
        sections = parse_markdown(content)

        assert len(sections) == 3
        assert sections[0]["header"] == "Main Title"
        assert sections[0]["level"] == 1
        assert sections[1]["header"] == "Section One"
        assert sections[1]["level"] == 2
        assert sections[2]["header"] == "Section Two"
        assert sections[2]["level"] == 2

    def test_parse_no_headers(self):
        """Test parsing markdown with no headers."""
        content = "Just some text without any headers."
        sections = parse_markdown(content)

        assert len(sections) == 1
        assert sections[0]["header"] == ""
        assert sections[0]["level"] == 0
        assert "Just some text" in sections[0]["content"]

    def test_parse_nested_headers(self):
        """Test parsing markdown with nested headers."""
        content = """# Level 1

## Level 2

### Level 3

#### Level 4
"""
        sections = parse_markdown(content)

        levels = [s["level"] for s in sections]
        assert levels == [1, 2, 3, 4]

    def test_parse_preserves_content(self):
        """Test that content under headers is preserved."""
        content = """## Section

- Bullet one
- Bullet two

Some paragraph text.

```python
code_block = True
```
"""
        sections = parse_markdown(content)

        assert "- Bullet one" in sections[0]["content"]
        assert "- Bullet two" in sections[0]["content"]
        assert "Some paragraph text" in sections[0]["content"]
        assert "code_block = True" in sections[0]["content"]

    def test_parse_empty_content(self):
        """Test parsing empty content."""
        sections = parse_markdown("")
        assert sections == []

    def test_parse_only_whitespace(self):
        """Test parsing whitespace-only content."""
        sections = parse_markdown("   \n\n   ")
        assert sections == []


# =============================================================================
# chunk_by_headers Tests
# =============================================================================

@pytest.mark.unit
class TestChunkByHeaders:
    """Tests for chunk_by_headers function."""

    def test_chunk_by_h2_headers(self):
        """Test chunking by H2 headers."""
        content = """## Introduction

This is the intro.

## Main Points

These are the main points.

## Conclusion

This is the conclusion.
"""
        chunks = chunk_by_headers(content)

        assert len(chunks) == 3
        assert "Introduction" in chunks[0]["header"]
        assert "Main Points" in chunks[1]["header"]
        assert "Conclusion" in chunks[2]["header"]

    def test_chunk_assigns_indices(self):
        """Test that chunk indices are assigned correctly."""
        content = """## First
Text

## Second
Text

## Third
Text
"""
        chunks = chunk_by_headers(content)

        for i, chunk in enumerate(chunks):
            assert chunk["chunk_index"] == i

    def test_chunk_no_headers_returns_single(self):
        """Test that content with no headers returns single chunk."""
        content = "Just a paragraph of text with no headers at all."
        chunks = chunk_by_headers(content)

        assert len(chunks) == 1
        assert chunks[0]["header"] == ""
        assert "Just a paragraph" in chunks[0]["content"]

    def test_chunk_skips_empty_sections(self):
        """Test that empty sections are skipped."""
        content = """## Has Content

Some content here.

## Empty Section

## Has Content Too

More content.
"""
        chunks = chunk_by_headers(content)
        # Empty section should be skipped
        contents = [c["content"] for c in chunks]
        assert all(c.strip() for c in contents)


# =============================================================================
# chunk_by_tokens Tests
# =============================================================================

@pytest.mark.unit
class TestChunkByTokens:
    """Tests for chunk_by_tokens function."""

    def test_chunk_short_content(self):
        """Test that short content returns single chunk."""
        content = "This is short content."
        chunks = chunk_by_tokens(content, chunk_size=500)

        assert len(chunks) == 1
        assert chunks[0]["content"] == content

    def test_chunk_long_content(self):
        """Test chunking long content."""
        # Create content with many words
        words = ["word"] * 1000
        content = " ".join(words)

        chunks = chunk_by_tokens(content, chunk_size=100, overlap=10)

        assert len(chunks) > 1
        for chunk in chunks:
            assert "word" in chunk["content"]

    def test_chunk_indices_sequential(self):
        """Test that chunk indices are sequential."""
        words = ["test"] * 500
        content = " ".join(words)

        chunks = chunk_by_tokens(content, chunk_size=50, overlap=5)

        for i, chunk in enumerate(chunks):
            assert chunk["chunk_index"] == i

    def test_chunk_empty_content(self):
        """Test chunking empty content."""
        chunks = chunk_by_tokens("")
        assert chunks == []

    def test_chunk_whitespace_only(self):
        """Test chunking whitespace-only content."""
        chunks = chunk_by_tokens("   \n\n   ")
        assert chunks == []

    def test_chunk_overlap_present(self):
        """Test that chunks have overlap."""
        # Create numbered content to verify overlap
        words = [f"word{i}" for i in range(100)]
        content = " ".join(words)

        chunks = chunk_by_tokens(content, chunk_size=50, overlap=10)

        if len(chunks) > 1:
            # Check that last words of chunk 0 appear in chunk 1
            chunk0_words = chunks[0]["content"].split()
            chunk1_words = chunks[1]["content"].split()
            # There should be some overlap
            chunk0_set = set(chunk0_words[-10:])
            chunk1_set = set(chunk1_words[:10])
            assert len(chunk0_set & chunk1_set) > 0


# =============================================================================
# chunk_document Tests
# =============================================================================

@pytest.mark.unit
class TestChunkDocument:
    """Tests for chunk_document main dispatcher."""

    def test_chunk_granola_document(self):
        """Test chunking a Granola document."""
        content = """---
granola_id: test123
---

## Meeting Summary

Summary content here.

## Action Items

- Item 1
- Item 2
"""
        chunks = chunk_document(content, is_granola=True)

        assert len(chunks) >= 2
        headers = [c.get("header", "") for c in chunks]
        assert any("Summary" in h for h in headers)
        assert any("Action" in h for h in headers)

    def test_chunk_short_document(self):
        """Test that short documents become single chunk."""
        content = "This is a short document without headers."
        chunks = chunk_document(content, chunk_size=500)

        assert len(chunks) == 1

    def test_chunk_long_document(self):
        """Test that long documents are chunked by tokens."""
        # Create long content
        words = ["paragraph"] * 2000
        content = " ".join(words)

        chunks = chunk_document(content, chunk_size=100)

        assert len(chunks) > 1

    def test_chunk_explicit_granola_flag(self):
        """Test explicit is_granola flag."""
        content = """## Section One

Content

## Section Two

More content
"""
        chunks = chunk_document(content, is_granola=True)

        # Should chunk by headers even without frontmatter
        assert len(chunks) >= 2

    def test_chunk_auto_detect_granola(self):
        """Test auto-detection of Granola via frontmatter."""
        content = """---
granola_id: auto-detected
---

## First

## Second
"""
        # Not passing is_granola, should detect from frontmatter
        chunks = chunk_document(content, is_granola=False)

        # Should still chunk by headers due to granola_id
        assert len(chunks) >= 2

    def test_chunk_all_have_chunk_index(self):
        """Test that all chunks have chunk_index."""
        content = """## One

Content

## Two

Content

## Three

Content
"""
        chunks = chunk_document(content, is_granola=True)

        for i, chunk in enumerate(chunks):
            assert "chunk_index" in chunk
            assert chunk["chunk_index"] == i


# =============================================================================
# _infer_topic Tests
# =============================================================================

@pytest.mark.unit
class TestInferTopic:
    """Tests for _infer_topic function."""

    def test_infer_from_h1_header(self):
        """Test inferring topic from H1 header."""
        content = "# Project Planning\n\nSome content here."
        topic = _infer_topic("notes.md", content)
        assert "Project Planning" in topic

    def test_infer_from_filename(self):
        """Test inferring topic from filename."""
        content = "No headers in this content."
        topic = _infer_topic("meeting-notes-team-sync.md", content)
        assert "meeting" in topic.lower() or "notes" in topic.lower()

    def test_infer_removes_date_pattern(self):
        """Test that date patterns are removed from filename."""
        content = "No headers."
        topic = _infer_topic("2026-01-30-project-update.md", content)
        # Date should be stripped
        assert "2026" not in topic
        assert "project" in topic.lower()

    def test_infer_truncates_long_topic(self):
        """Test that long topics are truncated."""
        content = "# " + "A" * 100 + "\n\nContent"
        topic = _infer_topic("file.md", content)
        assert len(topic) <= 50

    def test_infer_default_for_empty(self):
        """Test default topic for empty/generic content."""
        content = ""
        topic = _infer_topic(".md", content)
        assert topic == "general notes"


# =============================================================================
# generate_chunk_context Tests
# =============================================================================

@pytest.mark.unit
class TestGenerateChunkContext:
    """Tests for generate_chunk_context function."""

    def test_context_granola_meeting(self):
        """Test context generation for Granola meeting."""
        context = generate_chunk_context(
            file_path=Path("/vault/Meetings/team-sync.md"),
            metadata={
                "granola_id": "test123",
                "people": ["John", "Jane", "Bob"],
                "modified_date": "2026-01-30"
            },
            chunk_content="## Summary",
            chunk_index=0,
            total_chunks=2
        )

        assert "team-sync.md" in context
        assert "meeting notes" in context.lower()
        assert "John" in context
        assert "2026-01-30" in context
        assert "Part 1 of 2" in context

    def test_context_people_profile(self):
        """Test context generation for People profile."""
        context = generate_chunk_context(
            file_path=Path("/vault/People/John Smith.md"),
            metadata={},
            chunk_content="Contact info here",
            chunk_index=0,
            total_chunks=1
        )

        assert "John Smith.md" in context
        assert "personal profile" in context.lower()

    def test_context_daily_note(self):
        """Test context generation for daily note."""
        context = generate_chunk_context(
            file_path=Path("/vault/Daily/2026-01-30.md"),
            metadata={},
            chunk_content="Today's activities",
            chunk_index=0,
            total_chunks=1
        )

        assert "2026-01-30.md" in context
        assert "daily" in context.lower()

    def test_context_general_note(self):
        """Test context generation for general note."""
        context = generate_chunk_context(
            file_path=Path("/vault/Projects/project-alpha.md"),
            metadata={},
            chunk_content="# Project Alpha\n\nProject details",
            chunk_index=0,
            total_chunks=1
        )

        assert "project-alpha.md" in context
        assert "Projects/" in context

    def test_context_multi_chunk(self):
        """Test context includes part numbers for multi-chunk docs."""
        context = generate_chunk_context(
            file_path=Path("/vault/notes.md"),
            metadata={},
            chunk_content="Content",
            chunk_index=2,
            total_chunks=5
        )

        assert "Part 3 of 5" in context

    def test_context_single_chunk_no_part_number(self):
        """Test that single chunk docs don't show part number."""
        context = generate_chunk_context(
            file_path=Path("/vault/notes.md"),
            metadata={},
            chunk_content="Content",
            chunk_index=0,
            total_chunks=1
        )

        assert "Part" not in context


# =============================================================================
# add_context_to_chunks Tests
# =============================================================================

@pytest.mark.unit
class TestAddContextToChunks:
    """Tests for add_context_to_chunks function."""

    def test_add_context_to_all_chunks(self):
        """Test that context is added to all chunks."""
        chunks = [
            {"content": "First chunk", "chunk_index": 0},
            {"content": "Second chunk", "chunk_index": 1},
        ]

        result = add_context_to_chunks(
            chunks,
            file_path=Path("/vault/test.md"),
            metadata={}
        )

        assert len(result) == 2
        for chunk in result:
            assert chunk["has_context"] is True
            # Context should be prepended
            assert "test.md" in chunk["content"]

    def test_add_context_preserves_original_content(self):
        """Test that original content is preserved."""
        original_content = "Original chunk content here"
        chunks = [{"content": original_content, "chunk_index": 0}]

        result = add_context_to_chunks(
            chunks,
            file_path=Path("/vault/test.md"),
            metadata={}
        )

        assert original_content in result[0]["content"]

    def test_add_context_format(self):
        """Test context is separated by blank line."""
        chunks = [{"content": "Content", "chunk_index": 0}]

        result = add_context_to_chunks(
            chunks,
            file_path=Path("/vault/test.md"),
            metadata={}
        )

        # Should have context + blank line + content
        assert "\n\n" in result[0]["content"]


# =============================================================================
# Integration Tests
# =============================================================================

@pytest.mark.unit
class TestChunkerIntegration:
    """Integration tests for chunker workflows."""

    def test_full_granola_workflow(self):
        """Test complete Granola document chunking workflow."""
        content = """---
granola_id: meeting-123
people:
  - Alice
  - Bob
modified_date: 2026-01-30
---

## Meeting Summary

We discussed the Q1 roadmap and priorities.

## Action Items

- [ ] Alice to prepare specs
- [ ] Bob to review budget

## Next Steps

Follow up meeting scheduled for next week.
"""
        # Chunk the document
        chunks = chunk_document(content, is_granola=True)

        # Add context
        file_path = Path("/vault/Meetings/Q1-planning.md")
        metadata = {"granola_id": "meeting-123", "people": ["Alice", "Bob"]}

        contextualized = add_context_to_chunks(chunks, file_path, metadata)

        assert len(contextualized) >= 3
        for chunk in contextualized:
            assert chunk["has_context"]
            # Context should mention the meeting
            assert "Q1-planning.md" in chunk["content"]

    def test_full_long_document_workflow(self):
        """Test complete long document chunking workflow."""
        # Create a long document
        paragraphs = ["This is paragraph number {}.".format(i) for i in range(100)]
        content = "\n\n".join(paragraphs)

        # Chunk
        chunks = chunk_document(content, chunk_size=100)

        # Add context
        file_path = Path("/vault/Projects/research-notes.md")
        contextualized = add_context_to_chunks(chunks, file_path, {})

        assert len(contextualized) > 1
        for i, chunk in enumerate(contextualized):
            assert chunk["has_context"]
            assert f"Part {i+1}" in chunk["content"]
