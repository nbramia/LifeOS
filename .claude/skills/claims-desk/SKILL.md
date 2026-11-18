---
name: claims-desk
description: Claims desk procedure for insurance claims, FSA/HSA and employer reimbursements, and refunds or disputes. Use for the weekly reconciliation ("Claims desk: weekly reconciliation"), ad-hoc requests ("Claims desk: take care of X", "what's pending"), and approval replies such as "yes A1". Keeps one vault ledger that ties each deposit to the claim it settles.
---

# Claims Desk

The claims desk owns money that should come back to the user: insurance claims, FSA/HSA and employer reimbursements, refunds and disputes. It keeps one ledger in the vault, matches deposits to claims, drafts outbound actions for approval, and messages rarely.

Everything the desk knows about the user lives in the vault. The repo holds only this procedure. Examples below are synthetic ("Example Health Plan", "$123.45").

## Where it runs

A Claude Code session whose working directory is the vault root. The vault root is not a git repo, so the session creates no worktree, branch or PR. Never create one, and never write claim details into a git repo, GitHub issue, PR, commit message or log.

## The ledger

Find the ledger note with a vault search for `type: ledger` frontmatter; never assume its path. If none exists, say so and ask once before creating it.

Frontmatter:

- `always_ok:` list of action kinds the user approved as standing (for example `file-insurance-claim`).
- `last_message:` date of the last unprompted message.

Sections:

- **Open** — money that should come back: claim or request, payer, expected amount, date filed, deadline, status.
- **Awaiting approval** — drafted outbound actions with short ids (`A1`, `A2`, ...): id, kind, recipient, one-line summary, link to the draft.
- **Paid** — deposit date and amount, the claim(s) it settled, payer.
- **Unmatched deposits** — deposits that look like reimbursements but tie to no claim yet.

Editing rules:

- Edit with targeted section edits. Never rewrite the whole note; the user co-edits it.
- Keep ids stable. Never reuse an id.
- Move a row between sections instead of copying it. Each claim appears once.
- Record the date and source (email, portal, deposit) beside every status change.
- Keep claim documents in the vault next to the ledger; link them from the row.

## Sources

1. **Ledger and vault notes** — what is already known and what the user has said.
2. **Monarch** — `lifeos_monarch_transactions` for deposits (`lifeos_monarch_accounts` for context).
3. **Gmail** — `lifeos_gmail_search` for explanations of benefits, claim receipts, denial letters, refund confirmations and shipping or return notices.
4. **Portals** — insurer, benefit-card and merchant sites, through the login rules below. Use a portal only when email and the ledger cannot answer.

## Weekly reconciliation

Triggered by a task or schedule starting `Claims desk: weekly reconciliation`.

1. Read the ledger. Note `last_message` and every Open row with its deadline.
2. Pull deposits from Monarch since the last reconciliation (use the newest Paid date, or 60 days when Paid is empty).
3. Match deposits to Open claims (see Matching). Move matched claims to Paid; list leftover reimbursement-like deposits under Unmatched deposits.
4. Search Gmail since the last run for new explanations of benefits, denials, requests for information and refund notices. Update statuses. A denial stays in Open with status `denied` and a drafted appeal under Awaiting approval when an appeal is plausible.
5. Look for claims that should exist but do not: recent medical, pharmacy or reimbursable expense receipts with no ledger row. Add them to Open, and draft the filing under Awaiting approval.
6. Find stale rows: Open items with no movement past the payer's usual turnaround, or a deadline within 14 days. Draft a follow-up where one helps.
7. Check portals only for rows that email cannot settle, one attempt per portal.
8. Apply the messaging threshold. Update `last_message` if a message was sent.
9. Finish with a two-line summary in the session output. Do not message when the threshold is not met.

## Matching deposits

Match on amount, date window and payer, never on the aggregator's category. Bank aggregators mislabel reimbursements (a benefit-card payout can arrive categorized as a paycheck).

- Window: a deposit lands 1 to 30 days after the payer's "paid" date (explanation of benefits, portal status or email). Widen to 45 days for slow payers, and say so in the ledger row.
- Exact match: one deposit equals one claim's expected amount (`$123.45`), with a plausible payer or memo.
- Batched payout: one deposit equals the sum of several claims paid on or near the same date (for example `$123.45` + `$67.89` = `$191.34`). Search subsets of claims from the same payer inside the window; accept a subset only when the sum is exact to the cent and no other subset also fits. Record every claim the deposit settled.
- Partial payment: a deposit below the expected amount settles the claim only when the explanation of benefits shows the allowed amount or patient responsibility explains the difference. Otherwise leave the claim Open with the received amount noted.
- Ambiguous: more than one subset fits, or one claim fits two deposits. Do not guess. Put the deposit under Unmatched deposits with the candidate claims listed.
- Ignore deposits that match nothing and look like ordinary income.

## Ad-hoc requests

Triggered by a task starting `Claims desk:` followed by a request ("take care of X", "what's pending", "chase the refund for Y").

1. Read the ledger and the relevant sources.
2. Do all read-only work now: gather documents, check status, find deadlines, work out what is owed.
3. Draft every outbound action into Awaiting approval (see Approval flow). Do not send.
4. Record what was done and found in the ledger, and report a short summary as the session output.

A pure status question ("what's pending") is answered from the ledger without sources, unless the user asks for a fresh check.

## Approval flow

Anything sent to another person or organization is outbound: insurer, provider, merchant, dispute, support desk. That includes email, web forms, portal messages and chat.

- Draft it, store the draft in the vault, and add a row under Awaiting approval with the next free id (`A1`) and its kind (a short kebab-case label such as `file-insurance-claim`, `appeal-denial`, `request-refund`).
- Send only after the user approves. Approval arrives as a reply such as `yes A1` (recorded by the Finance persona, or found in the ledger). When it does, send exactly the approved draft, move the row to Open or Paid as appropriate, and note the date.
- If the kind is in `always_ok`, send without waiting, then record it in the ledger. The user adds a kind to `always_ok` only by saying "always"; the desk never adds one on its own.
- A change in recipient, amount or substance from the approved draft needs a new approval.
- Gmail drafts through `lifeos_gmail_draft` are the preferred form for email. Send through `lifeos_gmail_send` only after approval or under `always_ok`.

## Messaging threshold

Unprompted messages go to the Finance Telegram bot: `lifeos_telegram_send` with `{"text": "...", "bot": "finance"}`. Send one only when at least one of these is true:

- Money landed and was matched to a claim.
- A claim was denied.
- A deadline within 14 days needs the user.

At most one message a week: if `last_message` is within the last 7 days, hold the news until the next run. Batch everything into one short message, with amounts and the action needed, and no more detail than the ledger holds (for example: "$191.34 landed from Example Health Plan, settling 2 claims. 1 claim denied; appeal drafted as A2. Filing deadline for Example Pharmacy in 9 days."). Otherwise stay silent.

## Portal logins

Use the user-level `credentials` skill and the keymaster tools (`fill_login`, `wait_for_code`) in a Playwright browser.

- One attempt per portal per run. If keymaster refuses, has no item for the site, the account locks, or a CAPTCHA appears, stop. Record "portal unavailable" on the ledger row and, if it blocks a deadline, include it in the weekly message.
- Never change passwords, 2FA, recovery or payment settings, and never add or edit a bank account or card.
- Never write a credential, code or session token anywhere.
- Do not log out afterwards; other agents share the browser session.

## Privacy

Health and insurance details (providers, diagnoses, member ids, claim numbers, amounts) stay in the vault. They never go into git, GitHub issues or PRs, commit messages, logs, or the Telegram message beyond the minimum needed to act.
