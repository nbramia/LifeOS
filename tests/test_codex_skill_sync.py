"""Tests for converting LifeOS skills into Codex's skill format."""
from __future__ import annotations

from pathlib import Path

import pytest

from api.services.agent_worker.codex_skill_sync import (
    PORTABLE_SKILLS,
    install_skills,
    transform_skill,
)


pytestmark = pytest.mark.unit


_SAMPLE = """\
---
name: standup
description: Daily summary of shipped work
argument-hint: [hours]
---

# Standup

Summarize work for: $ARGUMENTS

## Context
- Current branch: !`git branch --show-current`
- Recent commits: !`git log --oneline -5`

Do the thing.
"""


def test_transform_keeps_name_and_description_drops_argument_hint():
    out = transform_skill(_SAMPLE)
    assert "name: standup" in out
    assert "description: Daily summary of shipped work" in out
    assert "argument-hint" not in out


def test_transform_rewrites_arguments_and_bash_injection():
    out = transform_skill(_SAMPLE)
    assert "$ARGUMENTS" not in out
    assert "the request you were given" in out
    # `` !`cmd` `` becomes an explicit run-this hint, not a pre-expanded value.
    assert "!`" not in out
    assert "(run `git branch --show-current` to get this)" in out
    assert "(run `git log --oneline -5` to get this)" in out


def test_transform_preserves_body_prose():
    out = transform_skill(_SAMPLE)
    assert "# Standup" in out
    assert "Do the thing." in out


def test_transform_raises_without_frontmatter():
    with pytest.raises(ValueError):
        transform_skill("# No frontmatter here\n")


def test_install_skills_writes_portable_set(tmp_path: Path):
    claude_dir = tmp_path / "claude_skills"
    codex_dir = tmp_path / "codex_skills"
    # Seed two portable skills + one that isn't on the allowlist.
    for name in ("standup", "pr-check", "implement"):
        d = claude_dir / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(_SAMPLE.replace("standup", name), encoding="utf-8")

    installed = install_skills(claude_dir, codex_dir)

    assert "standup" in installed
    assert "pr-check" in installed
    # Claude-orchestration skill is not on the allowlist → never installed.
    assert "implement" not in installed
    assert not (codex_dir / "implement").exists()
    # Converted file exists and is in Codex format.
    written = (codex_dir / "standup" / "SKILL.md").read_text(encoding="utf-8")
    assert "argument-hint" not in written
    assert "$ARGUMENTS" not in written


def test_install_skips_missing_sources(tmp_path: Path):
    """A partial .claude/skills checkout installs what it can without failing."""
    claude_dir = tmp_path / "claude_skills"
    codex_dir = tmp_path / "codex_skills"
    d = claude_dir / "standup"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(_SAMPLE, encoding="utf-8")

    installed = install_skills(claude_dir, codex_dir)
    assert installed == ["standup"]


def test_orchestration_skills_excluded_from_allowlist():
    for excluded in ("implement", "review-pr", "address-review", "tune", "mine-for-ideas"):
        assert excluded not in PORTABLE_SKILLS
