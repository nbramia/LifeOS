"""Shared redaction for untrusted text exposed outside the process."""
from __future__ import annotations

import re


_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"bot\d+:[A-Za-z0-9_-]+"), "bot<REDACTED>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "sk-<REDACTED>"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "ghp_<REDACTED>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "github_pat_<REDACTED>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA<REDACTED>"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer <REDACTED>"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "<REDACTED-HEX>"),
    (re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b"), "<REDACTED-TOKEN>"),
)


def scrub_secrets(text: str) -> str:
    """Redact common credential shapes from untrusted text."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def scrub_and_bound(text: str, max_chars: int = 300) -> str:
    """Redact credentials and cap the result to ``max_chars`` characters."""
    scrubbed = scrub_secrets(text)
    if len(scrubbed) <= max_chars:
        return scrubbed
    return scrubbed[: max_chars - 1].rstrip() + "…"
