# Claims Desk

> **Status:** Complete
> **Owner:** Finance persona
> **Last Updated:** 2026-11-18
> **Audience:** Operators

The claims desk is an agent that owns the user's insurance claims, FSA/HSA and employer reimbursements, and refunds or disputes. It keeps one ledger that ties each deposit to the claim it settles and shows what should have come back and hasn't. It messages rarely and is on call through the Finance persona.

Everything the desk knows about the user lives in the vault: the ledger, the claim records, and the list of action kinds the user has approved. The repo holds only the generic procedure, `.claude/skills/claims-desk/SKILL.md`.

## What it does

- **Weekly reconciliation.** Pulls deposits from Monarch and mail from Gmail, matches deposits to open claims (including batched payouts that sum several claims), updates statuses, and drafts filings or follow-ups for anything missing or stale.
- **Ad-hoc requests.** "Take care of X" through the Finance persona runs the same procedure for one item.
- **Approval flow.** Anything sent to another person or organization (insurer, provider, merchant, support desk) is drafted into the ledger's Awaiting approval section and sent only after the user approves, unless its kind is in `always_ok`.
- **Quiet by default.** Unprompted messages go to the Finance Telegram bot only when money landed and was matched, a claim was denied, or a deadline within 14 days needs the user. At most one a week.

## The ledger note

A vault note with `type: ledger` frontmatter. The desk finds it by search, so its path is not configured anywhere.

```markdown
---
type: ledger
always_ok: []
last_message:
---

## Open
## Awaiting approval
## Paid
## Unmatched deposits
```

| Section | Holds |
|---|---|
| Open | Money that should come back, with payer, expected amount, filing date and deadline |
| Awaiting approval | Drafted outbound actions with short ids (`A1`, `A2`) |
| Paid | Each deposit tied to the claim or claims it settled |
| Unmatched deposits | Reimbursement-like deposits with no claim yet |

`always_ok` lists action kinds the user approved as standing (for example `file-insurance-claim`). A kind enters the list only when the user says "always". `last_message` is the date of the last unprompted message; the desk reads it to enforce the weekly limit.

## Setup

1. Create the ledger note in the vault with the frontmatter and sections above.
2. Add a weekly schedule to `LifeOS/Scheduler/Inbox.md`:

   ```markdown
   - [ ] Claims desk: weekly reconciliation [cron:: 0 9 * * 6] [action:: agent] [mtype:: prompt] [working_dir:: <vault root>] #claude <!-- id:c1a2b3 -->
   ```

   The title is the prompt the session receives. `working_dir` is the vault root, which is not a git repo, so the Claude Code session creates no worktree, branch or PR. See [scheduler.md](scheduler.md).
3. Make sure the Finance bot is configured ([telegram-setup.md](telegram-setup.md)). The desk sends through `lifeos_telegram_send` with `bot: finance`.
4. Make sure keymaster has items for the insurer and benefit portals the desk should use. Portal logins use one attempt per portal; the desk stops on refusal, lockout or CAPTCHA and never changes passwords, 2FA, recovery or payment settings.

## Handing the desk work

- **From the Finance persona.** Say "take care of X". The persona records the request in the ledger and creates the task.
- **From another agent.** Create a task with `lifeos_task_create`, tag `claude`, a description that starts `Claims desk:` and says to follow the `claims-desk` skill, and `working_dir` set to the vault root in `fields`. Routing and the `#claude` tag are described in [agent-worker.md](../specs/product/agent-worker.md).
- **Approvals.** Reply `yes A1` to the Finance persona. The persona records the decision and creates a task so the desk sends the approved draft. `always` adds the item's kind to `always_ok`.

## Reading status

Read the ledger note. Open shows what is owed, Awaiting approval shows what needs a decision, and Paid and Unmatched deposits show what has landed. The Finance persona answers status questions from the ledger without re-checking sources.

## Privacy

Health and insurance details stay in the vault. They never go into git, GitHub issues or PRs, commit messages or logs.

## Related Documents

- [personas.md](personas.md) — The Finance persona that fronts the desk
- [scheduler.md](scheduler.md) — `action:: agent` schedules and the `working_dir` and `bot` fields
- [agent-worker.md](../specs/product/agent-worker.md) — The `#claude` tag and how the worker routes tasks
- [telegram-setup.md](telegram-setup.md) — The Finance bot that carries the desk's messages
- [human-queue.md](human-queue.md) — The separate queue for work only the operator can do
