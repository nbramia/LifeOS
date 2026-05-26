"""External agent worker for `#agent`-tagged tasks.

This package is the scaffolding for the agent worker described in epic #98.
Issue B (this issue) lands the plumbing: a long-running poll loop, atomic
task claiming, a session/transcript store, and a daily spend cap. Real LLM
execution arrives in Issues C and D; inter-agent coordination in E; Telegram
clarifications in F.
"""
