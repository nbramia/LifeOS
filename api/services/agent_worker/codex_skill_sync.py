"""Materialize LifeOS skills into Codex's local skill format.

LifeOS skills live in `.claude/skills/<name>/SKILL.md` and are authored in
Claude Code's slash-command dialect: YAML frontmatter with `argument-hint`, a
`$ARGUMENTS` placeholder, and `` !`cmd` `` bash-injection blocks in the Context
section. The portable subset is converted into Codex's plain `SKILL.md` format
and installed into `$CODEX_HOME/skills` (or `~/.codex/skills`) for local Codex
sessions and the LifeOS Codex worker.

This module converts the portable subset (no Claude subagent orchestration)
to Codex's format and installs them. It also installs native Codex skills
checked into `.agents/skills/` without conversion. It is import-safe and
side-effect-free until an install function is called, so transforms can be
unit-tested without touching the filesystem.

Only engine-agnostic skills are ported. The Claude-orchestration skills
(`review-pr`, `address-review`, `mine-for-ideas`) drive Claude's `Task`/`Skill`
subagent loop and are deliberately left Claude-only; `tune` edits the LifeOS
Claude-orchestrator internals and is also excluded. `implement` has a separate
native Codex rewrite under `.agents/skills/implement`.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import yaml


# Engine-agnostic skills safe to expose to Codex (verified free of Claude
# subagent-orchestration references). Keep this list conservative — when in
# doubt, leave a skill Claude-only rather than hand Codex instructions that
# reference primitives it doesn't have.
PORTABLE_SKILLS = (
    "standup",
    "catchup",
    "stale",
    "sync-health",
    "draft-issue",
    "pr-check",
    "merge-pr",
    "remove-worktree",
)

# Native Codex skills authored directly in Codex's SKILL.md format under
# `.agents/skills`. They are copied as-is so bundled resources/metadata survive.
NATIVE_CODEX_SKILLS = (
    "implement",
)

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
_BASH_INJECT_RE = re.compile(r"!`([^`]+)`")
# `argument-hint` values use Claude's bracket dialect (e.g. `[hours] [me|others]`)
# which isn't valid YAML, and we drop the key anyway — strip it before parsing.
_ARGUMENT_HINT_RE = re.compile(r"(?m)^argument-hint:.*\n?")


def transform_skill(text: str) -> str:
    """Convert one Claude `SKILL.md` body to Codex's format.

    - Keep only `name` and `description` in the frontmatter (drop
      `argument-hint`, which Codex doesn't use). The frontmatter is parsed as
      YAML so multi-line `description: >` / `description: |` folded scalars
      survive — re-emitted as a single (possibly long) line.
    - Rewrite `` !`cmd` `` bash-injection into an explicit "run this" hint;
      Codex has shell access and runs the command itself rather than having it
      pre-expanded. Done before the `$ARGUMENTS` swap so a command containing
      `$ARGUMENTS` isn't corrupted mid-rewrite.
    - Replace `$ARGUMENTS` with prose, since Codex skills aren't parameterized
      the same way — the user's request *is* the argument.

    Raises ValueError if the input has no parseable frontmatter or is missing
    the required `name` / `description` keys.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("SKILL.md has no YAML frontmatter")
    frontmatter, body = match.group(1), match.group(2)

    # Drop argument-hint first: its bracket dialect isn't valid YAML and would
    # break the parse. name/description are the only keys Codex needs.
    frontmatter = _ARGUMENT_HINT_RE.sub("", frontmatter)
    meta = yaml.safe_load(frontmatter) or {}
    name, description = meta.get("name"), meta.get("description")
    if not name or not description:
        raise ValueError("SKILL.md frontmatter is missing name or description")

    # Re-emit as valid YAML; width=inf keeps the (possibly long folded)
    # description on one line instead of re-wrapping it.
    new_frontmatter = yaml.safe_dump(
        {"name": name, "description": description},
        sort_keys=False, allow_unicode=True, width=float("inf"),
    ).strip()

    body = _BASH_INJECT_RE.sub(r"(run `\1` to get this)", body)
    body = body.replace("$ARGUMENTS", "the request you were given")

    return "---\n" + new_frontmatter + "\n---\n" + body


def _codex_skills_dir() -> Path:
    """Codex's skill discovery directory: $CODEX_HOME/skills, else ~/.codex/skills."""
    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return Path(codex_home) / "skills"


def install_skills(
    claude_skills_dir: Path | str,
    codex_skills_dir: Path | str | None = None,
    skills: tuple[str, ...] = PORTABLE_SKILLS,
) -> list[str]:
    """Write the portable skills into Codex's skill directory.

    Returns the list of skill names installed. Skips any allowlisted skill
    that has no source `SKILL.md` (rather than failing the whole run), so a
    partial `.claude/skills` checkout still installs what it can.
    """
    claude_dir = Path(claude_skills_dir)
    codex_dir = Path(codex_skills_dir) if codex_skills_dir is not None else _codex_skills_dir()

    installed: list[str] = []
    for name in skills:
        src = claude_dir / name / "SKILL.md"
        if not src.is_file():
            continue
        converted = transform_skill(src.read_text(encoding="utf-8"))
        dest_dir = codex_dir / name
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "SKILL.md").write_text(converted, encoding="utf-8")
        installed.append(name)
    return installed


def install_native_codex_skills(
    native_skills_dir: Path | str,
    codex_skills_dir: Path | str | None = None,
    skills: tuple[str, ...] = NATIVE_CODEX_SKILLS,
) -> list[str]:
    """Copy native Codex skills into Codex's local skill directory.

    Returns the list of skill names installed. Missing allowlisted sources are
    skipped so older checkouts can still install the converted portable set.
    Existing destination directories for these allowlisted skills are replaced
    to avoid stale resources after a source-side edit removes files.
    """
    native_dir = Path(native_skills_dir)
    codex_dir = Path(codex_skills_dir) if codex_skills_dir is not None else _codex_skills_dir()

    installed: list[str] = []
    for name in skills:
        src_dir = native_dir / name
        if not (src_dir / "SKILL.md").is_file():
            continue
        dest_dir = codex_dir / name
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.copytree(src_dir, dest_dir)
        installed.append(name)
    return installed
