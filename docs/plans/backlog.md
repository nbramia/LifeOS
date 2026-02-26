# LifeOS Backlog

> **Status:** Active
> **Last Updated:** 2026-02-19

Deferred features and enhancements to revisit.

- [ ] **Multi-step reasoning** - Allow Claude to fetch additional context if initial retrieval is insufficient.
- [x] **Proactive notifications** - Reminder scheduler with static, prompt, and endpoint message types delivered via Telegram.
- [x] **Slack/Telegram interface** - Telegram bot with full chat pipeline access, conversation management, and `/new`, `/status`, `/help` commands.
- [ ] **Time audit analysis** - Analyze Google Calendar to show where time actually goes vs stated priorities.
- [ ] **Connection discovery** - Find non-obvious relationships between notes, ideas, projects, and people using graph analysis.
- [ ] **Devil's advocate mode** - Given a position, construct strongest counterarguments informed by domain knowledge and user's specific context.
- [ ] **Agent-assisted entity resolution** - Use an agent to go person by person, identify potential merges, propose them for confirmation, then run the merge tool.
- [ ] **Emotion wheel visualization** - Shade emotions felt most often, or animate a gif lighting up emotions logged each day in the daily journal.
- [ ] **LinkedIn enrichment for inner circles** - Use Playwright or Claude in Chrome to document employment and educational history for contacts in circles 0-3 who have a LinkedIn URL.
- [ ] **Claude Code skills for LifeOS** - Create skills accessible to Claude Code that would also be available to the Telegram system when it spins up Claude Code sessions. Define reusable LifeOS capabilities as skills (e.g., meeting prep, daily briefing, relationship check-in) that work in both interactive sessions and Telegram-triggered workflows.
- [ ] **Split crm-ui.md** — `specs/product/crm-ui.md` is 1,649 lines (3x the 500-line target). Split into focused specs per CRM phase (people list, person detail, interaction timeline, etc.).

When people are categorized as unknown in photos, they still create source entities with the name of unknown. If we can group these together by similar face and then propose the ones that come up most often for naming, this could be a source of translating new people to their face. I could present myself with a quque for review after having some agent do grouping.

downgrade importance of work interactions a bit more

## Related Documents

- [Architecture](../specs/technical/architecture.md) -- System architecture and code structure
- [Data Model](../specs/product/data-model.md) -- Two-tier data model




