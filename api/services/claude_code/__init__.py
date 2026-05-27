"""Read-only adapter for local Claude Code CLI session data.

Surfaces transcripts at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`
in the same shape as the LifeOS agent worker so `/agents` can render both
sources side-by-side. See issue #144.
"""
