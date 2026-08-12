#!/usr/bin/env python3
"""Install the LifeOS skills into Codex's local skill directory.

Codex discovers skills from ``$CODEX_HOME/skills`` (or ``~/.codex/skills``) —
machine-local, like the Codex MCP config. This script converts portable LifeOS
skills from ``.claude/skills/`` into Codex's ``SKILL.md`` format, giving
``#codex`` agents the same workflow helpers as ``#claude`` where portable.

This script does NOT install the implementation lifecycle (`/implement`,
`/draft-issue`, `/pr-check`, `/merge-pr`, `/review-pr`, `/address-review`) —
those come from the `benjamcalvin/bootstraps` marketplace's
`implement-lifecycle` / `issue-management` plugins, installed separately
through Codex's own plugin browser. A fresh machine without that plugin
installed loses those six skills silently otherwise, so this script says so
on every run (see `main()`).

Re-run after editing the source skills. Idempotent.

Usage:
    ~/.venvs/lifeos/bin/python scripts/install_codex_skills.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.services.agent_worker.codex_skill_sync import install_skills  # noqa: E402

_LIFECYCLE_SKILLS = ("implement", "draft-issue", "pr-check", "merge-pr", "review-pr", "address-review")


def main() -> int:
    claude_skills = REPO_ROOT / ".claude" / "skills"
    if not claude_skills.is_dir():
        print(f"No skills source at {claude_skills}", file=sys.stderr)
        return 1
    installed = install_skills(claude_skills)
    if not installed:
        print("No portable skills found to install.", file=sys.stderr)
        return 1
    print(f"Installed {len(installed)} Codex skills: {', '.join(installed)}")
    print("Restart Codex to pick them up.")
    print(
        "NOTE: this script does not install the implementation lifecycle "
        f"({', '.join('/' + s for s in _LIFECYCLE_SKILLS)}). Those come from "
        "the benjamcalvin/bootstraps marketplace (implement-lifecycle, "
        "issue-management plugins) — install it through Codex's plugin "
        "browser if this machine doesn't have it yet, or those six commands "
        "won't exist for #codex."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
