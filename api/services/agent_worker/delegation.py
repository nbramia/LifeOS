"""Single source of the inter-agent delegation guidance injected into worker
executor system prompts (claude_code, codex, local).

Before this module the same spawn → monitor → read mechanic was copy-pasted
into three executors with slightly different wording, so renaming a
``lifeos_agent_*`` tool meant editing three files. The tool names and the
shared blurb now live here; each executor still composes its own framing
(recommended model, trigger, surrounding context) around the shared core.
"""

# lifeos_agent_* tool names — the rename-fragile surface the three executors
# share. Defined once here so a rename touches exactly one file.
SPAWN = "lifeos_agent_spawn"
CHECK = "lifeos_agent_check"
TRANSCRIPT_READ = "lifeos_agent_transcript_read"
SEND = "lifeos_agent_send"
SESSIONS_LIST = "lifeos_agent_sessions_list"
YIELD_UNTIL = "lifeos_agent_yield_until"


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
