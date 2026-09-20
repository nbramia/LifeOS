# Agent Activity Visualization (`/agents`)

> **Status:** Complete
> **Owner:** Agent Worker
> **Last Updated:** 2026-09-20

`/agents` is a Kanban board of the operator's work queue — vault tasks, agent questions, and scheduled work in one place, organized into lanes by status and tag. A **Graph** tab shows a deterministic delegation timeline as a secondary, read-mostly view for watching what's actively running: every LifeOS agent worker task (`#agent`-tagged), local CLI sessions discovered on the filesystem from both Claude Code (`~/.claude/projects/`) and Codex (`~/.codex/sessions/`), and Claude Code / Codex sessions registered from **any other machine** on the tailnet via a lightweight hook script.

The point is one place to see what needs attention: what's waiting on an assignment, what an agent is stuck asking about, what's scheduled to run next, and — when you want to watch the machinery — what's actually executing right now.

---

## Table of Contents

1. [Kanban board](#kanban-board)
2. [Projects](#projects)
3. [Graph tab — what you see](#graph-tab--what-you-see)
4. [Two sources, one graph](#two-sources-one-graph)
5. [Graph tab — Status semantics](#graph-tab--status-semantics)
6. [Graph tab — Filters and chips](#graph-tab--filters-and-chips)
7. [Graph tab — Side panel](#graph-tab--side-panel)
8. [Graph tab — Operator controls — kill](#graph-tab--operator-controls--kill)
9. [Graph tab — Operator controls — resume and Go To](#graph-tab--operator-controls--resume-and-go-to)
10. [Linking the board and the graph](#linking-the-board-and-the-graph)
11. [Privacy and exposure](#privacy-and-exposure)
12. [Configuration knobs](#configuration-knobs)
13. [Related Documents](#related-documents)

---

## Kanban board

The board is backed by the vault task store (`LifeOS/Tasks/`) — every card is a task, plus one card per upcoming scheduler entry. There is no separate "board" data file: a card's lane is always derived fresh from the task's status and tags, so editing a task from Obsidian, `/chat`, or a Telegram reply moves its card exactly as if it had been dragged.

## Projects

A project is an ordinary task with one or more children whose `parent_id` points at it. It has no stored project type: completed and cancelled children keep it a project, and removing the last child immediately restores the same task to its ordinary-card presentation. A former parent remains execution-paused after that transition until the operator explicitly resumes it, preventing an old assignment from starting unexpectedly.

Project cards show a compact resolved/total count; child cards keep their normal lane and actions while carrying a link back to their parent. The project filter can show projects, children, ordinary tasks, or all work. Counts are calculated before board filters, so hiding a child does not make progress look more complete than it is.

Opening a project uses a wider drawer. Its notes remain the objective and acceptance criteria, while the project section shows progress states, owner, coordination state and result, and an authoritative child list. Each child opens its normal drawer, can be assigned independently, and can be detached or moved to another project; the project drawer can also create a new linked child or attach an existing task by ID. Creating or attaching a child never silently copies the parent's assignment or starts work.

**Start project** marks a project active without pretending its owner executed the child work. **Plan and delegate** starts one bounded coordination run for the assigned owner; the drawer exposes that run's state, result, and session. **Complete project** requires every child to be resolved and any coordinator/cancellation work to be finished; closing with cancelled children asks for an explicit reduced-scope acknowledgement. **Cancel project** first previews unfinished, running, and awaiting-review work, then confirms a cascade that preserves completed work and pending-review output without accepting it. A partial cancellation remains visibly pending until its stop failures can be retried.

### Lanes

| Lane | What lands here |
|---|---|
| **Unassigned** | An open task with no assignee tag. |
| **Assigned** | An assignee tag is set (including `#me`) but work hasn't started. |
| **In progress** | Status `in_progress`, or the agent worker's own `#agent-running` tag. |
| **Human queue** | An agent is blocked on a question, or a `#human` card was filed for the operator, or the task's status is `blocked`. |
| **Scheduled** | A scheduler entry (`docs/guides/scheduler.md`) with at least one future fire. |
| **Review** | The agent worker's `#agent-completed` tag is set and the card hasn't been accepted yet. When the run that finished it has an outcome recorded (every top-level, non-spawned completion), the card face shows a compact pull-request badge — number and open/merged/closed — for a coding session that opened one; the drawer's **Agent outcome** section (below) shows the full record. |
| **Done** | Status `done` or `cancelled` (cancelled cards are hidden behind the "include cancelled" filter by default whenever the Done column is shown), plus scheduler entries that have fired (one-off) or been disabled (recurring). Hidden by default in the lane filter below — the least useful lane day to day. |
| **Snoozed** | The task carries a future wake-up time; the card returns to its natural lane on its own once that time passes, and LifeOS sends a Telegram notification naming the card. A snooze can never hide a running or finished card — it only applies while the card would otherwise be in Unassigned, Assigned, Human queue, or Review, so a card that starts running or finishes while snoozed surfaces immediately rather than staying hidden. Any Unassigned, Assigned, Human queue, or Review card can be snoozed; In progress, Done, and scheduler entries cannot. Set through the drawer's **Snooze** picker (or `PUT /api/agents/board/cards/{id}/snooze` directly) and cleared through **Unsnooze** (`DELETE` on the same path) or by dragging the card to another lane. Hidden by default in the lane filter below, the same as Done. |

Each lane header carries a small accent colour — the same palette the Graph tab uses for a node's fill, so a session's lane reads identically on both tabs.

### Human moves on agent-owned cards

A card is agent-owned once its assignee is `#claude`, `#codex`, `#hermes`, `#local`, or `#cloud`, OR the worker has already claimed it even with no assignee tag at all — the shape a legacy bare `#agent` queue card is left in. An agent-owned card is managed by the agent — dragging one is more restricted than dragging a `#me` or unassigned card, which can be dragged between every lane a human may drop a card into.

- **Before the worker claims it** (no `#agent-running`/`#agent-blocked` tag yet, and no CLI session opened on it — see below), a human may still reassign it, unassign it, or Cancel it (see below). A drag straight to In progress is refused — only the worker claims agent-assigned tasks — and so is a drag to Human queue or Done: those lanes exist for the worker to ask a question, finish, or get accepted into, not for a human to silently close or re-route a card that's been handed to an agent.
- **Once the worker claims it, OR a CLI session has been opened on it** (the drawer's **Open** action on an Assigned `#claude`/`#codex` card, before the worker itself ever adds `#agent-running`) — every drag is refused, and so is any change to the assignee, the Tags field, or the model/effort/host pickers in the drawer — each is disabled and shows the refusal reason as visible text rather than hiding. A pending Review card is never mistaken for claimed this way, even if it was opened via a CLI session earlier in its life. Focus (jump to a live CLI session's pane), Answer, Kill, Accept (once the card reaches Review), and Cancel remain the controls the drawer still offers when applicable; Cancel applies to a claimed card regardless of whether it carries an engine-specific assignee tag — a bare `#agent` card the worker claimed keeps Cancel as its one recovery action even though there's no assignee tag left to edit it back to a workable state. Refused controls render disabled with the reason visible next to them, except Cancel, which is hidden whenever its policy refuses the action.
- **Cancel** (see [Cards and the drawer](#cards-and-the-drawer)) works whether or not the worker has claimed the card, and is the one way to get rid of an agent-owned card the board otherwise won't let a human drag anywhere — except a card that's already finished (accepted-and-done, or already cancelled), which has nothing left to cancel.

A refused drag shows a toast with the reason instead of moving the card; the board and the drawer both refuse the exact same set of moves, computed by the same server-side rule so they can't disagree.

### Assignee

Assignee is a single tag, one of `#me`, `#claude`, `#codex`, `#hermes`, `#local`, `#cloud`. Dropping a card into Assigned sets that tag and clears any other assignee tag; dropping into Unassigned clears it. An engine assignee is the worker handoff, so a separate `#agent` tag is not required. The drawer's **Open** action can still start a CLI session immediately on an Assigned `#claude`/`#codex` card, and the worker reads the card's model/effort/host fields when it claims it (see [Card assignment](../technical/agent-worker.md#card-assignment)). The model picker's choice applies to both paths: a worker claim and an interactive Open both pass the card's model to the CLI when set, else the engine's catalog default.

The bottom of the Board tab keeps a touch-sized assignee tray with a Done target first, followed by one control per supported assignee. The assignee controls share two thirds of the tray's width on desktop. On a phone, Done and every assignee share one non-scrolling row of equal-width controls; all labels use a smaller type size, and any that don't fit end in an ellipsis, while their full names remain available as accessible labels and tooltips. The tray's own label and drop-status line share a fixed-height row above the controls so neither ever changes the tray's height mid-drag. Assignment works in either drag direction — a control dragged onto an eligible task card, or a card dragged onto a control — and both apply the same assignment write and policy checks as the drawer. Dragging is a mouse/pen gesture; on a touch screen the drawer's assignee picker is the assignment path. Every one of those paths clears any armed selection, so a control can never stay armed after an assignment and silently claim the next card. A successful assignment confirms with a toast carrying an **Undo** action that restores the card's previous lane and assignee. Lane drags and **Mark Done** — a drop onto Done under the hood — confirm the same way, and every one of these Undos restores the card's exact prior status and tags, not just the lane it was in: a card whose move stripped or added a tag (dragging a `#human` card to Done, for instance, drops that tag) gets it back exactly, and a card the move left in a different status is restored to that status rather than to a lane-derived one. Two moves keep their own dedicated restore instead: accepting a Review card (Done) is undone by reverting the acceptance rather than writing a status, and a card that was snoozed before the drag has its snooze re-applied once the rest of the restore completes. **Cancel** tears down the card's session subtree, so its toast says the action can't be undone rather than offering a link that would only restore a status over a stopped agent. A card the server lands in a lane other than the one requested gets only the toast naming where it really went — never a second one claiming the requested move. Refused targets expose the server-provided reason and do not write. When the Done lane is hidden, the tray also keeps a compact Done target available for permitted lane moves; dropping a Review card there uses the same acceptance transition as **Accept**.

### Cards and the drawer

A card shows its title, assignee chip, model/effort chips when the task carries those fields, host chips when the card is assigned to a machine other than the one running the API or a linked session ran somewhere, its other tags, and a pulsing dot when a linked session is actively running. The host chips carry two distinct meanings: an assigned-host chip when the card's host field names a machine other than the one running the API — the assignment, where the card will run — and a ran-on chip when a linked session ran somewhere and no assigned-host chip already names that host — the observation, where it did run. A card carrying both shows them distinguishably; a card assigned to another machine whose linked session ran on that same machine shows only the assigned-host chip; a card carries neither when it has no host field naming another machine and no linked session ran anywhere. Clicking a tag chip toggles the shared tag filter to exactly that tag (clicking the same chip again clears it), and the assignee and tag chips are each colored, spread evenly across the hue wheel so every distinct assignee and tag reads at a glance.

Clicking a card opens a drawer laid out as sections — a read-only metadata band, a read-only **Agent outcome** section when one exists, the editable fields, the action row, then the linked session. The metadata band reports the card's created and last-updated dates, its due date, and whichever lifecycle date applies to where it ended up (completed or cancelled), along with its status, any model/effort/host fields it carries, and its id; a date the card doesn't carry is omitted rather than rendered empty. The **Agent outcome** section — present on any card whose most recent run completed, not only a Review one — shows which engine ran it and when, the agent's own completion summary (the same text the operator's notification carried), and, for a coding session, the branch it worked on and each pull request it opened or referenced as a clickable link with a merge-status badge (open/merged/closed); a PR the background refresher hasn't reached yet shows no fabricated status rather than guessing. It's kept separate from — and rendered above — the editable Notes box: the outcome is a record of what happened, not something the operator edits, and a resumed session's later completion replaces it rather than appending another one. The editable fields are an editable title and notes (both save on blur; the title also saves on Enter, which commits the edit instead of inserting a line break since a title is a single-line value, and collapses any pasted newlines to spaces first — this applies on both task and Scheduled cards; notes are stored as indented `> ` lines beneath the task — see [task-management.md](task-management.md)), an assignee picker, and a searchable multi-select Tags picker, followed by the model, effort, and host pickers for engines that accept them — `claude`, `codex`, and `cloud` show a model picker (`cloud`'s options are the configured remote provider's model plus `LIFEOS_REMOTE_LLM_MODEL_OPTIONS`), and its unset ("") option reads "engine default (`<id>`)" once the catalog knows which model that engine currently defaults to, else "engine default". The Tags picker lists editable tags already used on the board, renders applied tags as individually removable chips, prevents duplicates, and offers **Create new** for normalized text that has no exact match. Confirming a tag — picking a suggestion, choosing Create new, or pressing Enter — adds it to whatever's already chosen; a token left typed but unconfirmed commits the same way on blur (one or several tokens, space- or comma-separated) or on the composer's Create, again adding to the existing selection rather than replacing it, while an unrecognized token is rejected with an inline toast naming it. Moving focus within the picker itself — onto a suggestion via the arrow keys, or onto a chip's own remove control — never commits a still-typed token; only leaving the picker, or Create, does. Assignee tags, Managed Agents executor tags (`#cloud-haiku`/`#cloud-sonnet`), and worker lifecycle tags (`#agent-running`, `#agent-blocked`, `#agent-completed`, `#agent-failed`, `#agent-budget-exceeded`, `#accepted`, `#agent-reassigned`) stay hidden and protected; every save re-reads those system-managed tags so concurrent worker changes survive. A failed tag save restores the last confirmed selection and reports the server reason. The remaining model/effort/host pickers write the fields the executors actually read; a picker save that fails snaps that picker back to its last-saved value unless the operator has already picked something newer on that same control while the failed save was in flight. The assignee select and all tag/assignment controls disable themselves with a visible reason whenever the card's current state forbids editing. The title field wraps onto as many lines as its text needs and grows with its content the same way, on every card kind, but with no height cap — a long title never scrolls horizontally or gets cut off. On a task card, the notes field grows with its content as you type (and when the drawer opens on a card with existing notes), up to two-thirds of the viewport height, after which it scrolls internally rather than growing further; a Scheduled card's message field (see [Scheduled column](#scheduled-column)) keeps a fixed box. When the card has a linked session, the drawer also shows that session's live transcript feed, the same panel the Graph tab uses. The drawer's action row and the Graph tab side panel's own action row are decided and rendered by the same logic, including **Accept** (move a Review card to Done). A successful Accept dismisses the drawer, keeps an underlined, keyboard- and touch-accessible **Undo** action in the toast for 1.5 times the standard toast duration, and Undo uses the server-authoritative transition to restore Review; a failed Undo reports the reason and refreshes from server state. Clicking anywhere outside the drawer — the board background, a lane, or another card — closes it exactly like its close button; a click inside the drawer never does. Escape closes the drawer, but does nothing while the New card composer, the Answer prompt, or the Delete confirmation is open on top of it. Graph Accept uses the same full close path, clearing the selected node, action row, panel body, and persistent graph-to-board selection hint before subsequent snapshots.

On narrow screens, the page stays at scale 1 with browser zoom disabled. The card drawer fits the viewport without horizontal overflow: its close button remains beside the title, long card ids and picker options wrap or shrink within the drawer, and action controls wrap inside its width. The Board and Graph tabs use the compact labels **B** and **G** while retaining their full accessible names. Board filters collapse behind an accessible disclosure by default; the disclosure summarizes active filters, and expanding it reveals the same controls and state. Native selects remain ordinary single tap/click controls. **Dragging is disabled for touch**: the lane strip scrolls horizontally, and that is the only way to reach another lane on a phone, so a card keeps both axes for the browser's own scrolling rather than reserving one for a drag. A touch gesture on a card is therefore either a scroll or a tap that opens the drawer; lane moves and assignment come from the drawer's own actions. Card and assignee-tray dragging with a mouse or pen uses Pointer Events, and `pointercancel` always clears the drag ghost, target styles, capture, and body state. The tray's fixed-width target row does not scroll.

A blocked card's existing **Answer** action is a note composer; submitting it deposits the note into the open question and lets the worker resume the prior session. A Review card also offers **Reject** (required note, resumes In progress) and **Reassign** (valid assignee plus optional context note, moves to Assigned while preserving whatever prior-run transcript and messages exist). Reject needs a prior session to resume and is shown disabled with that reason when the card has none; Reassign works either way. These actions are shared by the Board drawer and Graph session panel and report server-side failures without moving the card. Human queue cards label their manual completion action **Mark Done**.

Unassigned, Assigned, Human queue, and Review cards also offer **Snooze**. Clicking it opens a picker with three presets — **Later today** (three hours from now), **Tomorrow morning** (9am the next local day), and **Next week** (9am next Monday local time, or the Monday after if today already is one) — plus a custom duration (a number of minutes, hours, or days; the unit defaults to days) and a custom date-time. Every choice resolves in the browser to an absolute wake-up time carrying the browser's own UTC offset before it's sent; a custom time earlier than the current moment is refused with an inline message and never reaches the server. Snooze isn't offered on In progress, Done, or scheduler cards. A snoozed card shows its wake-up time on both the card and in the drawer, and offers **Unsnooze** in place of Snooze. Its drawer otherwise offers only lane-agnostic actions — such as Answer (when a question is pending), Rename, Cancel, and Delete, plus Go To and Resume when the card has a linked CLI session; lane-specific actions (Accept, Reject, Reassign, Mark Done) become available again once the card is unsnoozed, either by waking or by clicking Unsnooze. Dragging a snoozed card to another lane clears the snooze the same way any other move does; nothing can be dragged into the Snoozed lane itself, since snoozing only ever happens through the picker.

A **New card** button in the filter bar opens a composer — title, optional notes, the same Tags picker the drawer uses, a lane picker, and an assignee picker — that creates a task. Each visible lane also carries its own full-width **+** button above its cards, opening the same composer with that lane preselected. The composer's Tags picker offers the same suggestions, chips, and **Create new** affordance as the drawer's, hides the same assignee, Managed Agents executor, and worker lifecycle tags, and sends whatever is chosen in the create request instead of a separate save — there's no card to save against yet. A few rules govern how Lane and assignee interact:

- Picking an assignee while Lane still reads Unassigned flips Lane to Assigned, since a task carrying an assignee tag always files there regardless of what Lane says; manually overriding Lane back to Unassigned afterward doesn't change where the card lands.
- Clearing the assignee back to blank while Lane reads Assigned flips Lane back to Unassigned.
- Picking Assigned (from the top-bar button or a lane's own **+**) requires an assignee; the created card carries it as a tag, merged with any tags chosen in the picker with no duplicates.
- Picking In progress with an agent assignee (`#claude`/`#codex`/`#hermes`/`#local`) is rejected before anything is created — only `#me` can be assigned directly to In progress, since the worker claims agent-assigned tasks itself.
- Review, Scheduled, and Snoozed don't get a **+** — none of the three can be set directly; a card reaches Review or Scheduled the same way it always has (the worker's own tags, or the scheduler), and reaches Snoozed only through the drawer's Snooze picker.
- Creating a card straight into a lane the filter is currently hiding reveals that lane, updating the saved filter selection, so the new card is actually visible.

Scheduled carries its own **+**, above the column, that opens a separate schedule composer instead of the task composer above — Scheduled still can't be set directly, so the task composer's own Lane select still excludes it. The composer holds a Name field, an Enabled checkbox (on by default), a Timezone field (blank by default, meaning the configured default — not pre-filled from the browser's own locale), a trigger builder, an Action select (defaulting to `notify`), and — below the Action select — the same per-action sections the drawer renders (see [Scheduled column](#scheduled-column)). The trigger builder offers five modes: One-time (a date-time picker), Daily and Weekdays (a time picker, producing a daily or Mon–Fri cron expression), Custom days (day-of-week checkboxes plus a time picker, requiring at least one day), and Cron (a free-text cron expression). Switching from a generated mode (Daily, Weekdays, Custom days) to Cron prefills the cron field with the expression that mode was producing. Whenever the trigger or timezone changes, the composer fetches a live preview of the next three fire times — debounced so rapid edits don't flood the request, and omitting `timezone` from the request body while the field is blank so the server resolves its own configured default — and lists them formatted in the response's resolved timezone, or shows the preview API's error. Create stays disabled until Name is non-blank and the trigger is complete; clicking it posts the composed schedule, likewise omitting `timezone` while blank. A rejected create shows the server's error next to the field it names (the trigger, the timezone, an endpoint's method/path/params, the message, or the bot) or as a general error when none matches, and the composer stays open for another attempt. A successful create closes the composer, then scrolls to the new card and briefly highlights it, relaxing whichever shared filters currently hide it first — the same reveal behavior a graph "Show on board" or a `?card=` deep link uses.

### Multi-select

Holding Cmd (macOS) or Ctrl (other platforms) while clicking a task card toggles it into a selection instead of opening the drawer; modifier-clicking a selected card removes it. A selected card gets a purple outline. A plain click, or keyboard Enter/Space on a focused card, always clears the selection first, then opens that card's drawer as usual; Escape clears the selection when no drawer or modal is open, deferring to Escape's existing drawer-close behavior when one is (including while the drawer's own title field holds focus). Scheduled cards are never selectable — a modifier-click on one behaves exactly as a plain click does. A modifier-held press never starts a card drag. A card that drops out of the board on a live update (deleted, or moved out from under the filter) drops out of the selection too, and the count updates.

While at least one card is selected, a bottom action bar replaces the assignee tray (see [Assignee](#assignee)) — the tray is genuinely hidden, not just covered — and shows the count plus four actions and a clear control:

- **Delete** shows one confirmation naming the number of selected cards, then deletes each one through the same path as the drawer's Delete, including killing a live, killable session first on a card that has one.
- **Assign** opens a picker with the same assignee list the drawer uses, plus unassigned, and applies the choice to every selected card.
- **Tag** opens the same searchable picker the drawer's Tags field uses — existing board tags, or **Create new** — and adds that one tag to every selected card, preserving each card's existing tags (protected system tags included). It only adds; there is no bulk removal.
- **Mark Done** moves every selected card to Done: a Review card through the same acceptance transition as **Accept**, every other card through the plain lane-move endpoint. A card already in Done is a no-op.
- The clear control (and Escape) empties the selection without acting on it.

Each bulk action runs its write once per selected card, reading each card's current state at the moment the action runs rather than a stale copy captured when it was selected, and reports one summary toast naming how many succeeded; a card the server refuses (a claimed card an operator tries to reassign, for example) is left unchanged and named in the same toast alongside the server's reason, while every other card in the batch still goes through. There is no per-card confirmation and no per-card undo — Delete's one confirmation is the only prompt a bulk action shows. The four bar buttons disable for the duration of whichever action is running — including the time Delete's confirmation sits open awaiting a choice — so a second click can't start a second run of the same action or stack a second confirmation.

### Pending questions

When an agent asks a clarifying question, the card carrying that session shows the question text and an **Answer** button in the drawer. Answering writes the reply through the same path a Telegram reply takes — the worker resumes the session on its next tick exactly as if you'd answered by text.

A budget-breach question (the card is in Human queue, having hit its wall-clock, token, or dollar cap) also shows **Continue** and **Stop** buttons alongside Answer. Continue posts `yes` — doubling the breached cap and resuming the session from right where it left off — without opening Answer's free-text composer; Stop posts `stop`, ending the task with the `#agent-budget-exceeded` tag. Answer is still there for naming a specific new cap (`yes $12`, `yes 90 min`) instead of doubling it.

### Scheduled column

Each card shows the entry's next fire time, a recurring badge for cron entries, an action chip summarizing what firing it actually does, and — once it has fired at least once — the most recent run's outcome and a short result snippet. A **manual** schedule (no cron or one-off trigger) shows "Manual — trigger only" in place of a next fire time and carries no recurring badge, since it never fires on its own. The action chip reads `notify`, `prompt`, `endpoint: <METHOD> <path>` (the path truncated with an ellipsis beyond 40 characters, with the full path available as the chip's tooltip), or `agent: <executor>` (`agent: default` when the schedule carries no executor tag). The drawer edits the whole schedule, saving through the same `PUT /api/scheduler/{id}` the `/api/scheduler` UI uses — there's no separate write path for the board. Its shared fields are: name, an enabled checkbox, schedule type (cron, one-off, or manual) and the schedule value (a cron expression or an ISO datetime — the field's label and placeholder switch with the type, and the value input is hidden for manual, which has none), an IANA timezone, and the action (`notify`, `prompt`, `endpoint`, or `agent`).

Below the action select, the drawer shows exactly the inputs that action uses, rendered by a section shared with every client that edits a schedule's action-specific inputs. `notify` shows a message textarea labeled "Message (sent as-is)" and a bot picker. `prompt` shows a "Prompt (run through chat)" textarea, the same bot picker, and a hint that replying with `NO_ACTION` keeps the schedule silent. `endpoint` shows a method select (GET/POST), a route path input, a JSON params textarea, the bot picker, and a note that the route's own `scheduler_message` response field controls what's actually sent — it hides the message textarea entirely. `agent` shows a "Task description" textarea, an executor picker (an empty option meaning the schedule carries no executor tag and takes the agent worker's own default route), and a collapsed "Execution context" group (persona, model, effort, host, working directory) — it hides the bot picker. The bot picker lists exactly the names `GET /api/scheduler/bots` returns plus an empty "default (primary)" option, so no unaccepted name can be typed or chosen; a stored bot name absent from a loaded list still shows as a selected, distinctly labeled `(unknown)` option rather than leaving the picker blank, and a registry fetch failure disables the picker with the reason shown as visible text next to it (the stored name isn't known to be invalid in that case — just unconfirmed). Switching the action select swaps the visible section immediately, before any save, and without reopening the drawer.

Most fields save independently on blur (text) or change (selects, the checkbox). Schedule type and schedule value are the exception: changing the type only updates the field's label and placeholder locally, and the actual save — carrying both the type and the value together — happens on the value field's next blur, so a type change always reaches the server paired with a value. The endpoint method, path, and params save together as one `endpoint_config` write whenever any of the three changes, so the server always validates a complete, consistent call config rather than a partial edit. The params textarea validates as JSON locally on blur — invalid JSON or a value that isn't a JSON object shows a field-level error immediately and sends nothing to the server. A save the server rejects — an unparsable cron expression or datetime, an unknown timezone, an endpoint action missing a valid method/path/params, a notify/prompt/agent action left with a blank message — shows the response's detail inline next to the offending field(s) (or as a toast for the message, executor, bot, and execution-context fields) and reverts the control(s) to the last values the server accepted. Switching the action to one whose required inputs are missing is rejected the same way, leaving the schedule's stored action unchanged.

A human-readable next-fire preview updates from the response of any save that can change the next fire time — schedule type/value, timezone, or the enabled checkbox — and the drawer shows the last run's outcome; a manual schedule shows no next-fire preview at all. A **Trigger now** button fires the schedule immediately through `POST /api/scheduler/{id}/trigger` and refreshes the drawer's last-run line; for a `once` schedule its label discloses that firing consumes the schedule (the fire disables it and clears its next fire), while a `manual` schedule stays enabled and has no way to fire besides Trigger now. A failed trigger shows a toast with the reason. The drawer's Delete action (see [Cards and the drawer](#cards-and-the-drawer)) is offered here too, behind the same confirmation, and removes the entry through `DELETE /api/scheduler/{id}`.

### Filters

Which lanes show at all is a multi-select: a checkbox per lane in a dropdown, plus **All** and **Clear** controls (Clear resets to the default: every lane except Done and Snoozed). An unchecked lane's column is removed from the board entirely, not just emptied of cards, so the remaining lanes widen to fill the space; re-checking it puts it back in canonical lane order. The selection is remembered via `localStorage` (per browser/device, not synced) and restored on your next visit; if nothing at all is checked, the board shows a one-line hint instead of going blank. A saved selection that lists only lanes that existed when it was written never contains a newly added lane's id, so restoring it leaves that lane hidden exactly like a freshly-cleared selection would — no migration step is needed.

The rest of the filters AND-compose on top of whichever lanes are showing: free-text search (title and notes), assignee (including "me", "unassigned", and the cloud engine), host, engine, tag, recency, and whether to include cancelled cards. The host filter's option list names every host that appears either as a card's assigned host or as a linked session's host, and a card matches a selected host when either one names it — so a card assigned to a host that hasn't run a session yet is still reachable through the filter. The engine filter matches a card by its linked session's routing/execution engine; a card with no linked session matches only "all engines". A card's tag chip is a shortcut for the tag filter field: clicking it sets `tag` to that chip's text, or clears it if that tag is already the active filter, exactly as typing it into the tag filter would. Modified newest is the default sort, and the created/modified chronological choices use a normalized timestamp plus a deterministic id tie-breaker for every card type; missing timestamps participate in the ordering so switching newest/oldest exactly reverses each lane. An explicit sort choice is remembered in `localStorage` and never rewrites vault order. **Clear filters** resets all shared filters, lane visibility, cancelled inclusion, and sorting to modified newest. Search, lane, assignee, host, engine, tag, and recency are shared with the Graph tab's own filter bar — see [Linking the board and the graph](#linking-the-board-and-the-graph); "include cancelled" and sorting stay board-only. The board reads tasks from every context file but intentionally has no context-file filter; use tags such as `#work` to partition work. The board updates live — an edit made directly in the vault (or by the agent worker, or by the scheduler) shows up within a few seconds without a page reload.

On mobile, the filter disclosure defaults to collapsed to preserve lane space. Its summary reports whether shared or board-local filters are active, and opening it never resets the selected lanes, filter values, or sort choice.

### Out of scope

Manual card reordering within a lane. Display sorting is client-only and does not change vault order.

---

## Graph tab — what you see

The Graph tab is a deterministic delegation timeline. Each node is one session:

| Encoding | Meaning |
|---|---|
| **Fill colour** | The session's board lane (the same lane a linked task shows in on the Board tab) — a lane colour legend on the graph tab lists every lane and its swatch. A session's fill uses reduced opacity once it's terminal; the stroke stays the status colour, thicker for `blocked`. |
| **Shape** | Engine: square = Claude Code, hexagon = Codex, star = Hermes (also covers the `#cloud`/remote-provider path), diamond = Local, circle = Claude (the Managed Agents cloud model). A shape legend on the graph tab names all five. |
| **Size** | A monotonic function of `total_active_seconds` (floored and capped) — how long the session has actually been working, not how much it's cached. Two sessions with equal active time render the same size regardless of token counts. |
| **Secondary ring** | A thin accent ring around the node sized by tool-call count. |
| **Question badge** | A small ring + `?` glyph, offset from the label, when a pending question is open for the operator on this session. |
| **Error badge** | A count, offset from the label, when `error_count > 0`. |
| **Collapsed-subagent badge** | On a parent with completed child branches: `+N` for the currently-hidden direct-child count, or a plain collapse glyph once fully expanded. Click to toggle. |
| **White pulsing border** | Session is `running` AND has written to its transcript in the last 60 seconds (i.e. *actively producing output right now*) |
| **Edge** | Spawn relationship — parent → subagent. Hidden while the subagent side is collapsed. |
| **Horizontal position** | Earlier sessions appear to the left and later sessions to the right. Equal timestamps use a stable id tie-breaker, with parents always preceding their descendants. |
| **Vertical position** | Root sessions occupy level 0. Each generation of delegated work occupies the next level down. Dashed row guides label the depth explicitly. |

**Node label** — the text under each node, first non-empty of: an
operator-pinned custom label, the derived label (task description for
LifeOS, first non-empty user message for Claude Code — the same value a
linked board card shows as its title), the AI-generated short summary, the
most recent prompt preview (cross-machine CLI sessions), the routing name,
then the first 8 characters of the session id as a last resort. The
operator-pinned custom label, the derived label, and the AI-generated
short summary are each skipped when they're not a real label but the raw
id the row fell back to (the session id, that id with its `cc:`/`cx:` CLI
prefix stripped, or the row's task id). The model badge (`model_label`)
is never a candidate here — it renders only as a chip (the hover card, the
side panel's chip row, the Hermes routing badge), so two sessions on the
same model never read as the same node. A node never renders a bare `?`.

### Subagent trees

Active delegation branches remain expanded. Completed child branches are
collapsed by default; their parent shows the collapsed-subagent badge.
Clicking the badge expands those sessions and their delegation edges;
clicking again collapses them. Searching or deep-linking to a session
expands its ancestors automatically.

Machines and linked cards are session metadata, not graph nodes. Host stays
available as a filter and in the hover card and side panel; a linked card's
title labels the session and **Show on board** navigates to it. The graph
draws only actual parent-child delegation edges.

### Hover card and canvas controls

Hovering a node shows an HTML card near the cursor — name, host,
branch or cwd, model and effort, cost, duration, and the last event kind —
with no delay; moving off hides it.

- **Drag a node** — pins it where you drop it.
- **Drag the empty background** — pans the whole graph.
- **Scroll-wheel / pinch** — zooms in and out (0.2× – 5×).
- **Fit / Reset buttons** — Fit frames every visible node into the
  viewport; Reset returns to the default pan/zoom. On a large or crowded
  graph, fitting everything in can require zooming out far enough that a
  node's own label text would render too small to read at its normal
  size, so labels grow larger (their line spacing growing along with
  them, so lines and neighboring labels stay clear of each other) to hold
  a legible floor. Only past a point where no amount of enlarging holds
  that floor without labels overlapping are they hidden instead, and the
  hover card still names whichever node the operator points at. Reset
  always shows labels again, at their normal size.
- **Click a node** — opens its transcript in the side panel immediately
  (no artificial delay) and highlights its delegation tree (the selected
  node gets a thick white border, its connected lineage gets a thinner
  white border, and unrelated sessions dim).
- **Double-click a non-subagent Claude Code or Codex node** — jumps focus
  to its terminal (see [Operator controls — resume and Go To](#graph-tab--operator-controls--resume-and-go-to)); the side panel opening on the first click of the pair is expected.
- **Click the same node again, or click empty background** — deselects and closes the panel. Double-clicking a non-subagent Claude Code or Codex node is the exception: the pair's first click closes the panel and the double-click reopens it on that same session as focus jumps to its terminal.
- **Filter change** — resets the pan/zoom transform so the deterministic visible set returns to its natural scale.

An unchanged snapshot produces exactly the same coordinates. New sessions
are inserted according to creation time without any settling animation or
periodic movement.

---

## Two sources, one graph

The Graph tab unions three ingest paths into one rendered surface:

1. **LifeOS agent worker** — every `#agent` task the worker has claimed, plus its sleeps, yields, terminal outcomes, and any spawned children. This is the same data covered by [product/agent-worker.md](agent-worker.md); the viz is the read-side view of it.

2. **Claude Code CLI** — every transcript jsonl under `~/.claude/projects/`, scanned every snapshot tick (with a 30s cache so the disk isn't hammered). Each `.jsonl` file is one session; subagents spawned via the Task/Agent tool appear as separate nodes attached by spawn edges. Read-only.

3. **Codex CLI** — every rollout jsonl under `~/.codex/sessions/<year>/<month>/<day>/`, ingested the same way. One JSONL per session, `cx:`-prefixed in the snapshot. Read-only.

All three sources are normalized to the same shape before rendering, so filters, chips, and the side panel work identically on each kind of session. Disable an ingest path with `LIFEOS_CLAUDE_CODE_VIZ_ENABLED=false` or `LIFEOS_CODEX_VIZ_ENABLED=false` if you only want a subset.

Every session, from every source, carries a `host` field — the machine it's running on. LifeOS agent worker sessions and locally-scanned CLI transcripts always report the machine hosting the API; a session registered from elsewhere (see below) reports its own hostname.

### Cross-machine CLI session registration

A Claude Code or Codex session doesn't have to run on the machine hosting the API to show up here. `scripts/lifeos-agent-hook.sh`, installed for both CLIs by `scripts/install-agent-hooks.sh`, posts a lifecycle event on session start, prompt submit, stop, and session end to `POST /api/agents/cli-sessions/events` — from any machine on the tailnet, bearer-token authenticated. The API keeps a small `cli_sessions` record per session (host, cwd, branch, model, status, last prompt preview, and an optional task id read from `$LIFEOS_TASK_ID`) and merges it into the snapshot:

- A session with both a registration and a local transcript (the common case on the API host itself) collapses into **one row** — status comes from the registration events (accurate: `running` right after a prompt, `idle` after Stop, `ended` after SessionEnd), while token counts and dollar cost still come from the transcript.
- A session registered from a machine with no local transcript (every other machine) appears as its own row with `host` set to that machine's name, no token/cost detail (the hook doesn't read usage data), and status directly from the event stream — never inferred from file age.

This is opt-in: the endpoint is disabled (503) until an operator sets `LIFEOS_AGENT_HOOK_TOKEN`, and each machine needs the installer run once plus a small local env file with the API URL and that same token. See [guides/agents-go-to.md](../../guides/agents-go-to.md) for setup.

### Remote session parity

Registration alone gives a remote session status and a prompt preview, but
not the rest — token counts, dollar cost, tool-call counts, and the
transcript feed only exist for a jsonl this API host can read off its own
disk. For every host in the [operator's host registry](../../guides/agent-worker-setup.md#card-assignment-running-a-card-on-another-machine),
a background loop periodically pulls that host's Claude Code and Codex
transcript files onto this box over ssh (read-only, incremental — see
[technical/agent-viz.md](../technical/agent-viz.md#remote-transcript-mirror)
for the mechanism). The ingest scans those mirrored copies alongside the
local ones, so a remote session reaches full parity with a local one:
real tokens, cost, tool calls, and a live transcript feed in the drawer,
merged with the registration event's status the same way a local
transcript already merges (event status wins; token/cost detail stays
transcript-derived). A mirrored session's `running` status can only come
from a registration event, never from a process scan on this machine — a
transcript existing here doesn't mean the CLI is actually running here.

---

## Graph tab — Status semantics

A node's stroke colour is its status. The set is slightly different per source — same broad categories, different precise meaning:

| Status | LifeOS agent worker | CLI (Claude Code or Codex) |
|---|---|---|
| **running** | Currently executing tool calls or LLM turns. | A live `claude` / `codex` process is running with this jsonl's cwd, **or** the file was modified in the last 10 minutes. The first is authoritative; the second is inferred. |
| **claimed** | Worker has picked up the task but hasn't fired the executor yet (preflight is in flight). | n/a |
| **yielded** | Paused waiting for spawned children to finish. | n/a |
| **idle** | n/a | Registered via the session hook: open and waiting for input, after a `session_start` or `stop` event. Live, not finished. |
| **inactive** | n/a | Modified within 24h but no live process — typically you closed the terminal mid-session. Resumable. |
| **blocked** | Waiting on a Telegram clarification from you. | n/a |
| **completed** | Task ran to completion successfully. | jsonl is >24h old, no error in the last event. |
| **failed** | Executor crashed, preflight rejected, or runtime error. | Last event in the jsonl was an error/tool failure (and >24h old). |
| **ended** | n/a | Registered via the session hook: a `session_end` event was received — finished. Hidden by default like completed/failed; Resume available. |
| **budget_exceeded** | Token / wall / dollar cap breached and the session was killed externally. | n/a |

A small `(inferred)` hint appears next to the status on CLI sessions whenever the status came from mtime rather than from a confirmed live process — useful to know when reading "running" on a session you don't remember starting.

---

## Graph tab — Filters and chips

The top toolbar has filter controls and five count chips. **Filters are AND-composed**; the chips reflect *what's currently visible* after the filter, not the full snapshot. `recency`, `route` (engine), `lane`, `assignee`, `tag`, and the free-text search input are shared with the board's own filter bar — see [Linking the board and the graph](#linking-the-board-and-the-graph). `host` is board-shared too, via the same mechanism.

### Filters

| Filter | Default | Notes |
|---|---|---|
| `include finished` checkbox | off | Off → completed / failed / budget_exceeded / ended are hidden. On → those sessions show (subject to every other filter, `lane` excepted — see the `lane` row below), and the recency window auto-defaults wider. Graph-only — no board counterpart. |
| `recency` dropdown | auto: last 30 min, or 7 days once `include finished` is checked (1 min, 30 min, 1h, 24h, 7d, all also selectable) | Filters by `last_activity_at`. Shared with the board, whose own default is all time — the shared value starts unset so each tab keeps its own default until the operator (on either tab) picks one explicitly, which then applies on both. Board recency filters by `updated_at` instead — each tab keeps its own comparison field, only a chosen value is shared. |
| `cwd` dropdown | all | Only Claude Code sessions are scoped to a cwd. Dropdown lists every unique cwd present in the current snapshot; auto-hides when empty (no Claude Code sessions visible). Graph-only — no board counterpart. |
| `host` dropdown | all | Limit to sessions running on a specific machine. Dropdown lists every unique `host` present in the current snapshot; auto-hides on a single-host deployment (nothing to distinguish). |
| `route` dropdown | all (local / claude / claude_code / codex / hermes / remote / ask) | Filters by where the session ran — operator's local LLM, Managed Agents cloud, Claude Code CLI, Codex CLI, Hermes, the configured remote provider, or a session parked waiting on the operator. Labelled **engine** on the board's own bar. |
| `status` dropdown | all | Hard-filter by the status column from the table above. Graph-only — no board counterpart. |
| `lane` dropdown | every lane but Done | A single-select mirror of the board's own lane multi-select — `all` shows every lane, one lane shows just that lane. A session with no lane info (a fixture predating this field) is never excluded by it. The Done lane is the one exception: whether a Done-lane session renders is decided by `include finished` alone, never by this selection, so the two controls can't disagree about the same set of sessions — picking `done` here shows only Done-lane sessions and checks `include finished` automatically, since otherwise the selection would show nothing. |
| `assignee` dropdown | any assignee | Same options as the board's assignee filter — `unassigned` matches a session with no card assignee. |
| `tag` text input | empty | Substring match against the linked card's tags. |

### Chips

| Chip | What it counts |
|---|---|
| `running` | Visible sessions with status `running`. |
| `blocked` | Visible sessions waiting on Telegram clarification. |
| `recent` | Visible sessions with status `completed` or `ended`. |
| `cc` | Visible CLI sessions (Claude Code and Codex rolled together). |
| `API spend` | Sum of `total_dollars` across visible **LifeOS** sessions. Both CLIs are intentionally excluded — they're billed against your Claude Pro / ChatGPT subscriptions, not metered API tokens, so adding them would distort the chip's meaning. The per-session dollar columns on CLI nodes still show the equivalent API cost as a relative-cost signal. |

Chips re-compute after every snapshot tick, so toggling `include finished` immediately bumps the API-spend number to include the finished sessions' final cost.

---

## Graph tab — Side panel

Clicking any node opens a panel on the right with that session's metadata header and a live-tailing event feed. The panel header carries:

- **Label** — the same precedence chain the graph node uses (see **Node label** in [Graph tab — what you see](#graph-tab--what-you-see)), so the header never shows a session's raw id when the node or the search dropdown wouldn't. **Click it, or the action row's Rename button, to rename:** the title becomes a text box. It opens prepopulated with the current custom label or derived label, or empty when neither is a real name (only a raw id) — so blurring without typing never persists a raw id as the custom label. Enter (or clicking away) saves, Escape cancels. A manual name is pinned durably and overrides every other source everywhere the node is named (graph node, panel, search), except that a manual name identical to the row's own raw id is skipped by the graph node and the search dropdown the same way any other raw-id label is. Saving an empty value clears the override and reverts to auto-naming.
- **cwd** — Claude Code only; the project directory the session was opened in.
- **Branch** — the git branch of that cwd, when a registration event supplied one. Blank for sessions with no cross-machine registration (e.g. a local Claude Code transcript with no hook installed).
- **Status badge** — the status the node's stroke encodes, with `(inferred)` if applicable.
- **Source** — `LifeOS agent` or `Claude Code`.
- **Host badge** — the machine the session is running on.
- **Routing** — a plain badge, one of `Local`, `Claude Code`, `Codex`, `Remote`, `Hermes`, `Ask` (parked waiting on the operator, no model running), or `Claude` — never a model name, EXCEPT for a Hermes session that has taken at least one turn: its badge shows `model_label` (`Hermes · <model>`, the honest per-session attribution the server records once that session's own turn reports a model) instead of the plain `Hermes` name. A Hermes session with no turn yet, and every non-Hermes session, show exactly the plain routing name.
- **`.panel-chips` row** — small chips below the header: `model_label` and the engine name (from the same five-engine mapping the node's shape uses), each dropped when its text already equals the Routing badge's text above it, plus the effort when present. The host is not repeated here — the meta row's host badge already shows it. Display-only metadata, never the header's name.
- **Cost** — `total_dollars` to 4 decimals. For Claude Code, this is cache-aware accounting (separately tracking input, output, cache_creation @ 1.25× and cache_read @ 0.10×).
- **Tokens** — `input↓ / output↑`.
- **Depth badge** — if the session is a child, shows spawn depth.
- **Last prompt preview** — the most recent prompt submitted, truncated to 200 characters, when a registration event supplied one.

The event feed is newest-on-top. Backfill arrives first (the last 50 events by default), then live updates stream in via SSE. Each event has:

- A **kind label** — click to filter the feed to just that kind (e.g. `tool_call`, `user_message`, `failed`). Click again to clear. When a session first opens with any `user_message` events present, the filter auto-defaults to `user_message` to focus the view on the operator-visible turns.
- A **timestamp** in your local timezone.
- A **payload preview** — rendered as structured fields (model badge, routing decision, tool-call pills `Name(arg, arg)`, compact budget `90s · $0.30 · 500k tok`, usage summary `↓ in · ↑ out · cache read/create`, free-text fields like `ambiguity` / `question` / `reason`). Noisy fields (`iterations`, nested `ephemeral_*`, etc.) are suppressed. Click anywhere on the event to expand — the raw JSON appears beneath the structured view for diagnostics; click again to collapse.

The panel is **resizable**: drag the left edge to widen or narrow it. The chosen width is remembered across sessions via `localStorage`.

Clicking the same node again, the `×` button, or empty background area closes the panel and clears the graph selection. You can click another node directly to switch focus.

---

## Graph tab — Operator controls — kill

LifeOS agent sessions in non-terminal states get a red **Kill** button in the panel header. Clicking it opens a confirmation modal asking for an optional reason (logged to the transcript) before firing the request.

A kill takes down the target session **and every descendant in its subtree** — not the whole spawn root, only what hangs below the node you clicked:

- The target session gets an `operator_killed` transcript event.
- Each descendant gets a `cascade_killed` event.
- If the target was a Managed Agents (cloud) session, the worker process also tears down the remote session via the Anthropic API so you stop being billed for idle session-hours.
- The task in your vault transitions to whatever the worker writes as the post-kill tag (typically `#agent-failed`).

A CLI session the board opened gets a working **Kill**: it ends the pane that session runs in, and the card's session shows terminal. **Cancel** on such a card does the same before marking the card cancelled, so cancelling actually stops the work rather than leaving an agent running behind a closed card. A session recorded against another machine is reported instead — this API can only reach its own terminal — and so is a worker-spawned CLI session, which has no pane handle to end; for either, stop it in the terminal where it's running.

---

## Graph tab — Operator controls — resume and Go To

CLI sessions (Claude Code AND Codex) get up to two buttons:

**Resume** (shown on terminal / `inactive` / `yielded` sessions) opens a new WezTerm tab at the session's working directory and launches `claude --resume <session_id>` or `codex resume <session_id>` in it. WezTerm prints the new pane id; LifeOS stores it in a sidecar SQLite mapping (`data/cc_wezterm.db`) keyed by session id (`cc:` or `cx:` prefix disambiguates).

**Go To** (shown on every non-subagent CLI session, including live ones) jumps focus to the existing WezTerm pane for that session. Double-clicking the node in the graph does the same thing. The endpoint resolves the pane id in three steps:

1. **Cached mapping** — first checks `data/cc_wezterm.db`, populated by either a prior Resume click *or* the optional SessionStart hooks (`scripts/claude-session-pane.sh` for Claude Code, `scripts/codex-session-pane.sh` for Codex) which bind every new CLI start to its wezterm pane via `/api/agents/cc-pane-bind` and `/api/agents/cx-pane-bind` respectively. The cache is auto-invalidated when wezterm restarts: each mapping records the wezterm-gui pid it was written under, and a fresh wezterm boot drops the entry rather than blindly activating a stale `pane_id` (which could now belong to an unrelated session).
2. **FD probe** — if the cache misses, `lsof` finds which process holds the session's transcript file open; the holder's controlling TTY is matched against `wezterm cli list --format json`'s `tty_name`. Cwd is not enough to disambiguate when multiple panes share a project; the transcript file is. The result is cached for the next click.
3. **Activate-pane** — once a pane id is known, `wezterm cli activate-pane --pane-id <id>` switches focus. If WezTerm is the focused window the tab switches immediately; if it's hidden, the pane is selected in the background and a `notify-send` urgency hint pulses the dock icon. The OS-level window-raise across applications is restricted by Wayland compositors (no programmatic foreground steal); WezTerm under XWayland can be raised via `wmctrl`/`xdotool` if the operator needs it.

If a cached pane has gone stale (typical: user closed the tab), the activate-pane call fails, the mapping is cleared, and the probe runs once more — the session may have been resumed in a fresh pane. Only when both the cache *and* a fresh probe come up empty does Go To return 404 (toast: "Couldn't locate pane — install the SessionStart hook if claude is running in wezterm"); when a pane existed but is gone and no replacement can be found it returns 410.

Resume + Go To are **off by default** because spawning GUI terminals from a systemd service depends on the operator's desktop environment. Enable with `LIFEOS_CC_RESUME_ENABLED=true` for Claude Code sessions and `LIFEOS_CODEX_RESUME_ENABLED=true` for Codex; each flag also gates Go To for its respective source. Customize launchers via `LIFEOS_CC_RESUME_CMD` / `LIFEOS_CODEX_RESUME_CMD` if you don't use WezTerm — substitutions `{cwd}`, `{cwd_url}`, `{session_id}`, `{session_id_url}`, and `{inner_command}` are available. The probe-based Go To is WezTerm-specific (it reads `wezterm cli list`'s `tty_name`); non-WezTerm launchers can still use Resume but Go To will respond 404.

A session registered from another host (see "Cross-machine CLI session registration" above) resumes and focuses over ssh when that host is one of the operator's registered hosts (see [Card assignment](../technical/agent-worker.md#card-assignment)) — the same launcher runs remotely, so Resume and Go To work wherever the session actually lives. Only a host the operator hasn't registered still 409s: the error names that host, so the operator knows to go there instead of getting a silent no-op or a misleading 404.

### Resume here

Next to Resume, the drawer offers a small host picker listing this API's
own machine plus every registered host — "resume here" lets the operator
choose where the session should actually open, regardless of which
machine it originally ran on:

- Choosing this API's own machine or a registered host launches there over
  the same mechanism as above (locally, or over ssh), overriding the
  session's recorded host.
- Choosing a machine that isn't this API host and isn't registered can't
  be launched from here — the drawer instead shows the exact resume
  command (`cd <cwd> && claude --resume <id>` or the Codex equivalent) as
  copyable text, so the operator can paste it into a terminal on that
  machine themselves. The command is omitted when the session's cwd
  can't be resolved — there's nothing to show.

---

## Linking the board and the graph

The board and the graph describe the same work from two angles, kept in one navigational state.

**Card session chip → graph.** A task card whose linked session exists shows a small clickable session chip (↗ session) among its other chips; clicking it switches to the Graph tab, pans to that session's node, and selects it.

**Node → board.** Selecting a node shows a **Show on board** action above the transcript panel whenever the session is linked to a card. Clicking it — or simply switching to the Board tab while that node stays selected — scrolls to the card and briefly highlights it, relaxing whichever shared filters (lane, assignee, host, engine, tag, search, recency) currently hide it first.

**Answer from the graph.** A selected node with an open pending question shows the same **Answer** action as the board drawer and posts through the same endpoint.

**Deep links.** `/agents?session=<id>` opens the Graph tab with that session selected and centred; `/agents?card=<id>` opens the Board tab with that card's drawer open. An id that doesn't resolve — no such session, no such card — shows a toast and leaves the default view.

**Shared filters.** Search, lane, assignee, host, engine, tag, and recency are one filter state shared by both tabs' filter bars — changing one on either tab updates the other, and the selection persists across reloads (`localStorage`, per browser/device). A **Clear** button on each bar resets every shared filter to its default. The board's own lane multi-select IS this shared state's lane selection; the graph's own lane filter is a single-select (`all`, or one lane) reading and writing the same selection — picking one lane there sets the shared selection to just that lane, and picking `all` sets it to every lane. The board's assignee/host/tag/recency filters and the graph's `route` filter (labelled **engine** on the board's own bar) are the remaining five shared keys — an engine match is by the session's routing/execution engine, so a card with no linked session only matches "all engines". Each tab still keeps one filter of its own with no counterpart to share: the board's "include cancelled" checkbox, and the graph's include-finished toggle, cwd filter, and status filter.

---

## Privacy and exposure

- The Resume/Go To/kill primitives act only on **this** machine and on a registered host over ssh (see below); the local transcript scan reads only this machine's own transcript directories. Cross-machine visibility is otherwise opt-in and one-directional: another machine's hook posts a small lifecycle event (host, cwd, branch, status, a truncated prompt preview) to this API.
- The transcript mirror is the one path where this API reads files from another machine: for each host in `LIFEOS_AGENT_HOSTS` (empty by default), unless `LIFEOS_AGENT_TRANSCRIPT_MIRROR_ENABLED` is disabled, it pulls that host's Claude Code and Codex transcripts read-only over ssh onto this box. Nothing is ever written back to a remote host.
- The registration endpoint (`POST /api/agents/cli-sessions/events`) is bearer-token gated and disabled by default (503 until `LIFEOS_AGENT_HOOK_TOKEN` is set) — unlike the kill/resume endpoints below, it's meant to be reachable over Tailscale, since that's the whole point.
- Transcript payloads are truncated to 240 chars in the feed previews — click an event to see the full payload only on demand. A registered session's prompt preview is truncated to 200 characters at the source.
- The kill, resume, and pane-bind (`/cc-pane-bind`, `/cx-pane-bind`) endpoints are **local-network only**. They must not be exposed via Tailscale Funnel or the public MCP HTTP transport (the gates live in [api/routes/agents.py](../../../api/routes/agents.py); see the technical spec for the threat model). Resume and Go To act on sessions recorded as running on this API's own host directly, and on a registered host over ssh; a session on an unregistered host returns an error naming that host instead.
- Claude Code ingest is strictly read-only — LifeOS opens jsonl files for reading and never writes back, whether the file lives on this machine or in the transcript mirror.

---

## Configuration knobs

All in `.env`. None are required — the defaults work for the standard LifeOS install.

| Var | Purpose | Default |
|---|---|---|
| `LIFEOS_CLAUDE_CODE_VIZ_ENABLED` | Surface Claude Code CLI sessions alongside agent worker sessions. Set false to scope the viz to LifeOS sessions only. | `true` |
| `LIFEOS_CLAUDE_CODE_PROJECTS_DIR` | Where to find Claude Code transcripts. | `~/.claude/projects` |
| `LIFEOS_CLAUDE_CODE_LOOKBACK_DAYS` | Discovery window — older jsonl files are excluded from the snapshot (they can still be loaded by direct session id). | `7` |
| `LIFEOS_CC_RESUME_ENABLED` | Enable the Resume and Focus buttons, and the board drawer's **Open** action for `#claude` cards. | `false` |
| `LIFEOS_CC_RESUME_CMD` | Launcher command. Substitutions: `{session_id}`, `{cwd}`, `{session_id_url}`, `{cwd_url}`, `{inner_command}`. The default uses WezTerm's CLI to open a tab AND run the resume in one shot. | `wezterm cli spawn --cwd {cwd} -- {inner_command}` |
| `LIFEOS_CC_RESUME_INNER_CMD` | The command run *inside* the spawned terminal — the actual `claude --resume` invocation. Substituted into `{inner_command}` of the launcher template. | `claude --dangerously-skip-permissions --resume {session_id}` |
| `LIFEOS_CC_RESUME_ENV_FILE` | Optional `key=value` file pinning `DISPLAY` / `XAUTHORITY` / `WAYLAND_DISPLAY` / `DBUS_SESSION_BUS_ADDRESS` for the spawned terminal. | `` (inherit systemd env) |
| `LIFEOS_CODEX_VIZ_ENABLED` | Surface Codex CLI sessions alongside the other sources. | `true` |
| `LIFEOS_CODEX_SESSIONS_DIR` | Where to find Codex rollout JSONLs. | `~/.codex/sessions` |
| `LIFEOS_CODEX_LOOKBACK_DAYS` | Discovery window for Codex rollouts. | `7` |
| `LIFEOS_CODEX_RESUME_ENABLED` | Enable Resume + Go To for `cx:` sessions, and the board drawer's **Open** action for `#codex` cards. | `false` |
| `LIFEOS_CODEX_RESUME_CMD` | Codex launcher template. Same substitution surface as `LIFEOS_CC_RESUME_CMD`. | `wezterm cli spawn --cwd {cwd} -- {inner_command}` |
| `LIFEOS_CODEX_RESUME_INNER_CMD` | Inner command inside the spawned terminal — the actual `codex resume` invocation. | `codex resume {session_id}` |
| `LIFEOS_AGENT_HOOK_TOKEN` | Bearer token required from `scripts/lifeos-agent-hook.sh` on `POST /api/agents/cli-sessions/events`. Empty (default) disables the endpoint (503) — a fresh clone accepts no cross-machine session data until this is set. | `` |
| `LIFEOS_AGENT_TRANSCRIPT_MIRROR_ENABLED` | Enable the remote transcript mirror loop. Safe on by default — with no hosts in `LIFEOS_AGENT_HOSTS` it never runs anything. | `true` |
| `LIFEOS_AGENT_TRANSCRIPT_MIRROR_DIR` | Local directory the mirror writes into, one subdirectory per registered host. A relative path resolves against the repo root, not the process's working directory. | `data/agent-transcript-mirror` |
| `LIFEOS_AGENT_TRANSCRIPT_MIRROR_INTERVAL_SECONDS` | How often each registered host's transcripts are re-pulled. Each pull is incremental, so a short interval costs little when nothing changed. | `120` |

---

## Related Documents

- [API Reference](api-reference.md) — HTTP contracts for the board's lane, accept, and cancel endpoints
- [ADR-011: External Agent Ingest](../../adr/011-external-agent-ingest.md) — Why Claude Code sessions surface read-only via a foreign-schema adapter
- [ADR-026: A Budget Breach Asks; It Does Not Fail](../../adr/026-budget-breach-asks.md) — Why a `budget` question gets Continue/Stop on the card
- [Agent Viz — Technical](../technical/agent-viz.md) — Endpoint shapes, delegation timeline layout, status inference rules, security boundaries, and the board's lane-derivation rules
- [Agent Worker](agent-worker.md) — The other half of the picture: how `#agent` tasks get claimed and run
- [Claude Code Orchestration (product)](claude-code-orchestration.md) — The orchestrator that spawns the Claude Code sessions surfaced here
- [Agent Worker — Technical](../technical/agent-worker.md) — Sessions, transcripts, kill primitives
- [Architecture](../technical/architecture.md) — Where the viz fits in the broader code structure
- [Task Management](task-management.md) — The vault task store the board's cards are backed by
- [Human Queue](../../guides/human-queue.md) — How `#human` cards are filed and auto-resolved by agents and the nightly sync
- [Scheduler Guide](../../guides/scheduler.md) — How the Scheduled column's entries are created and edited
