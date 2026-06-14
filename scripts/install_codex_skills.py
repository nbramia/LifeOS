#!/usr/bin/env python3
"""Install the LifeOS skills into Codex's local skill directory.

Codex discovers skills from ``$CODEX_HOME/skills`` (or ``~/.codex/skills``) —
machine-local, like the Codex MCP config. This script converts portable LifeOS
skills from ``.claude/skills/`` into Codex's ``SKILL.md`` format and also copies
native Codex skills from ``.agents/skills/``. That gives ``#codex`` agents the
same workflow helpers as ``#claude`` where portable, plus Codex-specific
rewrites for workflows whose Claude version depends on Claude-only primitives.

Re-run after editing the source skills. Idempotent.

Usage:
    ~/.venvs/lifeos/bin/python scripts/install_codex_skills.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.services.agent_worker.codex_skill_sync import (  # noqa: E402
    install_native_codex_skills,
    install_skills,
)


def main() -> int:
    claude_skills = REPO_ROOT / ".claude" / "skills"
    if not claude_skills.is_dir():
        print(f"No skills source at {claude_skills}", file=sys.stderr)
        return 1
    native_skills = REPO_ROOT / ".agents" / "skills"
    installed = install_skills(claude_skills)
    installed.extend(install_native_codex_skills(native_skills))
    if not installed:
        print("No portable skills found to install.", file=sys.stderr)
        return 1
    print(f"Installed {len(installed)} Codex skills: {', '.join(installed)}")
    print("Restart Codex to pick them up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
