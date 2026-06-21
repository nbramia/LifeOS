This directory contains operational guides — how to set up, configure, and run LifeOS.

## Contents

- `installation.md` — Initial installation and dependency setup
- `setup.md` — Project setup and environment configuration
- `first-run.md` — First-time startup and verification
- `configuration.md` — Environment variables and settings reference
- `google-oauth.md` — Google OAuth setup for Gmail and Calendar
- `slack-integration.md` — Slack workspace integration
- `launchd-setup.md` — macOS launchd service configuration
- `scheduler.md` — Scheduler (triggers + actions): cron/one-off, notify/prompt/endpoint/agent
- `scripts.md` — Available scripts and their usage
- `apple-health.md` — Apple Health/Fitness ingestion (HealthBridge app + iOS Shortcut fallback)
- `troubleshooting.md` — Common issues and solutions
- `claude-code-orchestration.md` — Claude Code multi-agent orchestration patterns
- `doctor-bot.md` — Self-repair Telegram bot: report a problem → issue → implement → ship
- `agent-worker-setup.md` — External agent worker prerequisites (Gemma swap, MCP HTTP transport, Cloudflare Tunnel, bearer token)
- `agents-go-to.md` — /agents "Go To" wezterm pane setup (SessionStart hook + FD probe)

## Key Principles

- Guides are **instructional** — how to do X, not why X was chosen.
- Audience field is required in frontmatter (New users, Operators, or Developers).
- Include exact commands that can be copy-pasted.
- Test all commands before documenting them.

## Related Documents

- [Documentation Strategy](../AGENTS.md) — Rules governing all documentation
- [Installation](installation.md) — Start here for new setup
