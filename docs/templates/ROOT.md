# ROOT — Personal LifeOS Map

This is the navigation map for the personal knowledge system. It is an index,
not the source of truth: claims belong in the linked domain notes and should
retain their original source and confidence.

## Current direction

- Current state: capture and review meaningful life information through Telegram.
- Ideal state: a private, provider-independent cognitive layer that helps Amir
  remember, understand, prioritize, and act.
- Current focus: reliable capture, projects, commitments, relationships, and
  the next concrete action.

## Navigation

- [[LifeOS/Tasks/Dashboard]] — active tasks and next actions
- [[LifeOS/Tasks/Inbox]] — task captures awaiting processing
- [[LifeOS/Scheduler/Dashboard]] — reminders and scheduled follow-ups
- [[LifeOS/Scheduler/Inbox]] — scheduling captures awaiting processing
- `~/.lifeos/inbox.json` — raw Life Inbox captures, review status, proposals, and provenance
- `~/.lifeos/life_model.json` — structured identity, values, current state, ideal state, and philosophy records
- [[Personal/Identity]] — identity, values, philosophy, and preferences
- [[Personal/Goals]] — goals and ideal-state direction
- [[Personal/Projects]] — active and potential projects
- [[Personal/People]] — people and relationship context
- [[Personal/Knowledge]] — durable knowledge and learning
- [[Personal/Ideas]] — ideas and someday/maybe items
- [[Personal/Decisions]] — important decisions and rationale
- [[Personal/Sources]] — source notes and provenance
- [[Personal/Routines]] — routines and recurring practices

## Capture rules

1. Treat incoming material as raw data until interpreted.
2. Preserve source, timestamp, and uncertainty for important items.
3. Separate memories, ideas, goals, projects, tasks, reminders, and decisions.
4. Do not turn a tentative thought into a commitment without evidence.
5. Review the Life Inbox weekly and either process or dismiss each item. The configured
   Telegram deployment runs this review every Sunday at 10:00 in its configured timezone.
6. Keep explicit statements about identity and direction in the structured life model;
   label inferences as inferences and preserve their source.

## Provider independence

Personal data in this vault is permanent. LLM providers and models are replaceable
processing infrastructure; changing them must not require a memory migration.
