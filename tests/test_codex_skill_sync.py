"""Tests for converting LifeOS skills into Codex's skill format."""
from __future__ import annotations

from pathlib import Path

import pytest

from api.services.agent_worker import codex_skill_sync
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
    for name in ("standup", "catchup", "implement"):
        d = claude_dir / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(_SAMPLE.replace("standup", name), encoding="utf-8")

    installed = install_skills(claude_dir, codex_dir)

    assert "standup" in installed
    assert "catchup" in installed
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


def test_lifecycle_skills_excluded_from_allowlist():
    """The implementation lifecycle ships as the `benjamcalvin/bootstraps`
    plugin, installed in Codex through its own plugin browser. Listing those
    skills here would point the converter at sources this repo does not hold."""
    for excluded in ("draft-issue", "pr-check", "merge-pr"):
        assert excluded not in PORTABLE_SKILLS


def test_every_portable_skill_has_a_source_in_the_repo():
    """`install_skills` skips a missing source silently, so an allowlist entry
    with no `.claude/skills/<name>/SKILL.md` never reaches Codex and never
    raises. Pin the allowlist to skills that this repo actually carries."""
    repo_skills = Path(__file__).resolve().parent.parent / ".claude" / "skills"
    missing = [n for n in PORTABLE_SKILLS if not (repo_skills / n / "SKILL.md").is_file()]
    assert not missing, f"allowlisted skills with no source: {missing}"


def test_native_codex_skill_path_removed():
    """The native-skill install path (NATIVE_CODEX_SKILLS +
    install_native_codex_skills) was retired along with the six lifecycle
    directories it copied from `.agents/skills/` — Codex now gets the
    lifecycle from the `benjamcalvin/bootstraps` marketplace instead (#491).
    Guard against either resurfacing without a source in the repo.
    """
    assert not hasattr(codex_skill_sync, "NATIVE_CODEX_SKILLS")
    assert not hasattr(codex_skill_sync, "install_native_codex_skills")

    agents_skills = Path(__file__).resolve().parent.parent / ".agents" / "skills"
    retired = {"implement", "draft-issue", "pr-check", "merge-pr", "review-pr", "address-review"}
    # A missing `.agents/skills/` (e.g. a future cleanup removes the directory
    # entirely) satisfies "these six are absent" just as well as an existing
    # directory that lacks them — don't let iterdir() on a gone path fail the test.
    existing = {p.name for p in agents_skills.iterdir() if p.is_dir()} if agents_skills.is_dir() else set()
    assert not (existing & retired), f"retired lifecycle skills still present: {existing & retired}"
