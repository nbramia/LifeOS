"""Single source of the inter-agent delegation guidance injected into worker
executor system prompts (claude_code, codex, local).

The tool names and the shared blurb live here as a single source of truth,
so renaming a ``lifeos_agent_*`` tool means editing one file instead of
duplicating the spawn → monitor → read mechanic's wording across the three
executors. Each executor still composes its own framing (recommended
model, trigger, surrounding context) around the shared core.
"""

# lifeos_agent_* tool names — the rename-fragile surface the three executors
# share. Defined once here so a rename touches exactly one file.
SPAWN = "lifeos_agent_spawn"
CHECK = "lifeos_agent_check"
TRANSCRIPT_READ = "lifeos_agent_transcript_read"
SEND = "lifeos_agent_send"
SESSIONS_LIST = "lifeos_agent_sessions_list"
YIELD_UNTIL = "lifeos_agent_yield_until"
PROJECT_HANDOFF = "lifeos_agent_project_handoff"
TASK_CHILDREN = "lifeos_task_children"
PROJECT_PLAN = "lifeos_project_plan"
PROJECT_COMPLETE = "lifeos_project_complete"
PROJECT_CANCEL = "lifeos_project_cancel"


# Shared opening-prompt fragment. Executors import this verbatim so every
# route distinguishes durable board work from ephemeral session delegation.
PROJECT_TASK_GUIDANCE = f"""\
<project_tasks>
LifeOS has two separate delegation mechanisms. `{SPAWN}` creates an ephemeral
session child in your session lineage; it is not a board task and does not
create project hierarchy. Durable project children are ordinary LifeOS tasks:
inspect them with `{TASK_CHILDREN}` and give each an explicit, independent
assignment and execution request. Never assume a child inherits your route,
model, host, working directory, or provider consent; omitted assignment stays
unassigned, and cloud/provider access is allowed only within the already
authorized lineage scope. A project-child create or update from an agent
never carries #hermes — the operator assigns that from the board — and a
paid route (#cloud/#cloud-haiku/#cloud-sonnet) is refused unless the
project's own owner already carries that same route.

A durable hierarchy has exactly one child level. Use stable child keys and
inspect the existing children before retrying or creating work. A child cannot
become a project. For an existing project, use `{PROJECT_PLAN}` to run one
bounded coordinator session; it is not an always-on monitor and does not wake
automatically for every child completion.

If you are the current executor of an ordinary live top-level task and need to
turn that task into a project, call `{PROJECT_HANDOFF}` once with a stable
operation id and its bounded child plan. This is a terminal action for your
current turn: do not call regular attach/create-to-parent flows, do not keep
working afterward, and do not report the original task as completed. The
worker releases staged children only after it has observed your turn stop.
Pending handoffs remain pending if that stop cannot be proved.

Projects finish only through `{PROJECT_COMPLETE}` after their children and
reviews are resolved. Use `{PROJECT_CANCEL}` for project cancellation; it
cascades only after confirmation and may remain pending while a runtime stop
is unverified. Cancelling one child does not cancel its parent or siblings.
</project_tasks>"""


def delegation_preamble(session_id: str, *, trigger: str, model: str) -> str:
    """The compact, session-aware delegation blurb used by the claude_code and
    codex executors.

    Args:
        session_id: the caller's LifeOS session id, embedded so the agent can
            pass ``caller_session_id`` when it spawns a child.
        trigger: the condition that should prompt a hand-off, phrased as a
            sentence lead-in ending right before "delegate it…" — e.g.
            ``"To run background work in parallel,"`` or ``"If a task needs a
            capability you lack — e.g. browser/GUI automation you can't perform
            headlessly —"``.
        model: the ``model=`` value to suggest for the child, inserted verbatim
            (including quotes and any inline note) — e.g. ``'"local" or
            "claude"'`` or ``'"claude_code" for the browser-enabled Claude Code
            CLI'``.
    """
    return (
        f"Your LifeOS agent session id is {session_id}. {trigger} delegate it "
        f"with the `{SPAWN}` MCP tool (caller_session_id={session_id}, "
        f"model={model}). Monitor the child with `{CHECK}` and read its result "
        f"with `{TRANSCRIPT_READ}`. If a child's output contains "
        f"\"[needs clarification] …\", it stopped mid-task to ask you a "
        f"question: answer with `{SEND}` (session_id=child, message=answer) — "
        f"this reopens the child with its full prior context — then wait on it "
        f"again with `{YIELD_UNTIL}`. Send the answer before yielding; a "
        f"yielded session can't send."
    )


# Richer inter-agent protocol block for the autonomous local (Gemma) worker,
# which can also message peers and yield (instead of polling) while children run.
INTER_AGENT_BLOCK = f"""\
<inter_agent>
Other agent sessions are visible via `{TRANSCRIPT_READ}` and
`{SESSIONS_LIST}`. Spawn child agents with `{SPAWN}`,
message them with `{SEND}`, check status with
`{CHECK}`. When you have nothing to do until specific children
finish, call `{YIELD_UNTIL}(children=[...])` — this ends your
session cleanly (no idle billing) and resumes you when the children are
done. Prefer `yield_until` over polling. If a child's output contains
"[needs clarification] …", it stopped mid-task to ask you a question:
answer with `{SEND}` (this reopens the child with its full prior
context), then yield on it again — send the answer before yielding.
</inter_agent>"""
