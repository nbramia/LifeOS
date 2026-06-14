"""Tests for converting LifeOS skills into Codex's skill format."""
from __future__ import annotations

from pathlib import Path

import pytest

from api.services.agent_worker.codex_skill_sync import (
    NATIVE_CODEX_SKILLS,
    PORTABLE_SKILLS,
    install_native_codex_skills,
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


_FOLDED = """\
---
name: draft-issue
description: >
  Draft and create a GitHub issue from a description or investigation.
  Use when a problem is too large for a quick fix, or the user says
  "file an issue".
argument-hint: <issue description>
---

# Draft Issue

Body.
"""


def test_transform_preserves_folded_scalar_description():
    """A multi-line `description: >` must survive — Codex uses description for
    skill triggering, so an empty one makes the skill undiscoverable."""
    out = transform_skill(_FOLDED)
    assert "Draft and create a GitHub issue" in out
    assert 'file an issue' in out
    # No dangling YAML folded-scalar marker, no argument-hint.
    assert "description: >" not in out
    assert "argument-hint" not in out


def test_transform_raises_on_empty_description():
    text = "---\nname: x\ndescription:\nargument-hint: y\n---\nbody\n"
    with pytest.raises(ValueError):
        transform_skill(text)


def test_real_portable_skills_convert_with_nonempty_description():
    """Guard against silently shipping an untriggerable skill: every portable
    skill in the repo must convert with a non-empty description line."""
    repo_skills = Path(__file__).resolve().parent.parent / ".claude" / "skills"
    for name in PORTABLE_SKILLS:
        src = repo_skills / name / "SKILL.md"
        if not src.is_file():
            continue
        out = transform_skill(src.read_text(encoding="utf-8"))
        desc_line = next(
            (ln for ln in out.splitlines() if ln.startswith("description:")), ""
        )
        assert len(desc_line) > len("description: ") + 10, f"{name} has a thin description"


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


def test_install_native_codex_skills_copies_skill_directory(tmp_path: Path):
    native_dir = tmp_path / "native_skills"
    codex_dir = tmp_path / "codex_skills"
    skill_dir = native_dir / "implement"
    agents_dir = skill_dir / "agents"
    agents_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: implement\ndescription: Native Codex lifecycle.\n---\nBody.\n",
        encoding="utf-8",
    )
    (agents_dir / "openai.yaml").write_text(
        'interface:\n  display_name: "Implement"\n',
        encoding="utf-8",
    )

    installed = install_native_codex_skills(native_dir, codex_dir)

    assert installed == ["implement"]
    assert (codex_dir / "implement" / "SKILL.md").read_text(encoding="utf-8").endswith("Body.\n")
    assert (codex_dir / "implement" / "agents" / "openai.yaml").is_file()


def test_orchestration_skills_excluded_from_allowlist():
    for excluded in ("implement", "review-pr", "address-review", "tune", "mine-for-ideas"):
        assert excluded not in PORTABLE_SKILLS


def test_native_codex_skills_include_implement_only():
    assert NATIVE_CODEX_SKILLS == ("implement",)
