"""E0 -- build the shared per-turn dataset for the Jev orchestrator experiments.

Joins api/services/perf_trace.py's data/perf_traces.db (span-level latency
and tool-call timing) against data/conversations.db (persisted message
text, tool names+args via the `routing` column) into one row per agentic
chat turn.

Output (both under data/jev_eval/, gitignored -- see data/.gitignore /
top-level .gitignore's `data/` entry):
  - e0_dataset.jsonl  -- one JSON object per turn, WITH message text. This
    file is the input to E1-E5. Never commit it, never print its contents
    beyond small counts/paraphrases.
  - e0_results.json   -- join-quality stats and corrected baseline numbers
    (counts only, safe to quote in the report).

Data-availability notes (read before trusting a field):
  * perf_traces.db has no tool call arguments or results -- only span
    names (`tool_{name}`) and durations. Args come from conversations.db's
    `routing.sources` (ordered tool NAMES for the whole turn, all rounds
    concatenated, including errored calls) and the separate `sources`
    display column (tool name + args, but only for a hardcoded subset of
    tools in chat.py's `_source_type_map`, truncated to 80 chars, and
    ERRORED calls are dropped). Neither column has tool RESULT content --
    it only ever existed in the in-memory AgentResult.tool_calls_log,
    never persisted. E2 works around this with the display `sources`
    field where available; see e2_inloop.py's docstring.
  * conversations.db's `routing.tool_rounds` is a misnomer in the
    production code (api/routes/chat.py:1341): it is
    `len(agent_result.tool_calls_log)`, i.e. total tool CALLS across the
    whole turn, not the number of LLM rounds. This script reports both:
    `tool_calls_total` (that column) and `rounds` (recomputed from
    perf_traces span names, the only source that actually has round
    boundaries: each `llm_api_round_N` span begins round N; any
    `tool_{name}` span appended after it and before `llm_api_round_{N+1}`
    belongs to round N).
  * perf_traces.db only has rows from 2026-02-16 onward; conversations.db
    goes back to 2026-01-08. Turns before 2026-02-16 cannot join and are
    excluded from anything needing span data (rounds, per-round tool
    lists, latency).
  * The "next user message looked like pushback" field is populated with
    the PRODUCTION regex classifier already shipped in agent_loop.py
    (`_PUSHBACK_PATTERNS`, gated on the assistant reply matching
    `_REFUSAL_PATTERNS`) rather than a new Jev call: escalation handling is
    not one of the judgments these experiments measure or gate, so
    spending Jev budget re-deriving it isn't justified. It's carried
    through only as a descriptive field for the report, not a Jev
    judgment.

Join logic: for each conversation, walk messages in arrival order. Each
user message that is followed (before the next user message) by an
assistant message whose `routing.reasoning` starts with "agentic" is one
turn (this excludes ack-only, clarification, and Claude Code/Codex
handoff turns -- they never entered run_agent_loop, so there's nothing
to judge). Find the perf_traces row with the same conversation_id whose
`question` equals the user message's content (perf_trace.start_trace
truncates to 200 chars, so compare on that truncation) and whose
created_at falls in [user_msg.created_at, next_user_msg.created_at) --
this disambiguates repeated identical questions in one conversation. A
turn that finds no such trace is still emitted (message-level fields are
still useful for E1/E3) but flagged `joined: false` and carries no
round/latency fields.
"""
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# data/ is gitignored and lives only in the main checkout, not in this
# worktree -- the eval writes ONLY here, so datasets never land under the
# worktree or anywhere that could be committed.
DATA_ROOT = Path("/home/nathanramia/Code/LifeOS/data")
OUT_DIR = DATA_ROOT / "jev_eval"
OUT_DIR.mkdir(parents=True, exist_ok=True)

PERF_DB = DATA_ROOT / "perf_traces.db"
CONV_DB = DATA_ROOT / "conversations.db"

# Mirrors api/services/agent_loop.py's escalation regexes (out of scope for
# the ship/drop judgments computed here -- see module docstring).
_REFUSAL_PATTERNS = re.compile(
    r"(?i)("
    r"(hasn'?t|has not|haven'?t|have not)\s+(yet\s+)?(been\s+)?"
    r"(released|announced|published|scheduled|finalized|determined|set|made public|come out)"
    r"|not\s+(yet\s+)?(been\s+)?(released|announced|published|scheduled|finalized|determined|available|out)"
    r"|isn'?t\s+(yet\s+)?(available|out|released|published|finalized)"
    r"|aren'?t\s+(yet\s+)?(available|released|published)"
    r")"
)
_PUSHBACK_PATTERNS = re.compile(
    r"(?i)("
    r"do (the )?research|do more research"
    r"|you'?re wrong|that'?s wrong|that'?s (not|in)correct|that'?s not (true|right)"
    r"|look it up|search (for it|again|the web|online)|try again|check again"
    r"|it should be possible|it is possible|yes it (has|is|did|does)"
    r"|i'?m telling you|i know (it|they|you|for a fact)"
    r"|that'?s not true|actually,? (it|that|they|the)"
    r"|they have been|it has been (released|announced|published)"
    r")"
)


def _norm_ts(ts: str) -> str:
    """Normalize the two timestamp spellings (space vs 'T' separator) to
    a common sortable/comparable string."""
    return ts.replace("T", " ") if ts else ts


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def load_traces():
    con = sqlite3.connect(f"file:{PERF_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT trace_id, conversation_id, question, model_tier, total_ms, created_at, span_data FROM traces"
    ).fetchall()
    con.close()
    traces = []
    for r in rows:
        spans = json.loads(r["span_data"]) if r["span_data"] else []
        traces.append({
            "trace_id": r["trace_id"],
            "conversation_id": r["conversation_id"],
            "question": r["question"] or "",
            "model_tier": r["model_tier"],
            "total_ms": r["total_ms"],
            "created_at": _norm_ts(r["created_at"]),
            "spans": spans,
        })
    return traces


def rounds_from_spans(spans: list[dict]) -> tuple[int, dict[int, list[str]], dict[int, float]]:
    """Return (max_round_reached, {round_num: [tool_name, ...]}, {round_num: llm_call_ms}).

    Spans are appended chronologically by trace_span() (see
    api/services/perf_trace.py). A `llm_api_round_N` span opens round N;
    any `tool_{name}` span seen after it (until the next
    `llm_api_round_*`) belongs to round N. Older persisted traces (before
    the round span was renamed from the Anthropic-specific
    `claude_api_round_N` -- see ADR-024's remote-backend generalization)
    still use the old name; both are matched here so the whole history
    parses, not just turns since the rename. The `llm_api_round_N` span's
    own duration is the LLM call latency for that round (used by E5 to
    approximate when the first tool round ends, for the shadow-call race
    simulation).
    """
    round_re = re.compile(r"^(?:llm|claude)_api_round_(\d+)$")
    current_round = 0
    max_round = 0
    tools_by_round: dict[int, list[str]] = {}
    round_llm_ms: dict[int, float] = {}
    for s in spans:
        name = s.get("name", "")
        m = round_re.match(name)
        if m:
            current_round = int(m.group(1))
            max_round = max(max_round, current_round)
            round_llm_ms[current_round] = s.get("duration_ms", 0.0)
            continue
        if name.startswith("tool_") and current_round:
            tool_name = name[len("tool_"):]
            tools_by_round.setdefault(current_round, []).append(tool_name)
    return max_round, tools_by_round, round_llm_ms


def repeated_tool_across_rounds(tools_by_round: dict[int, list[str]]) -> bool:
    """True iff some tool name was called in >=2 DISTINCT rounds (not just
    >=2 times within one round, e.g. two parallel person_info calls)."""
    round_sets = [set(v) for v in tools_by_round.values()]
    seen = set()
    for s in round_sets:
        for name in s:
            if name in seen:
                return True
            seen.add(name)
    return False


def load_conversation_turns():
    con = sqlite3.connect(f"file:{CONV_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    convs = {r["id"]: r["persona_id"] for r in con.execute("SELECT id, persona_id FROM conversations")}
    msgs_by_conv: dict[str, list[sqlite3.Row]] = {}
    for r in con.execute("SELECT id, conversation_id, role, content, sources, routing, created_at FROM messages ORDER BY conversation_id, created_at, id"):
        msgs_by_conv.setdefault(r["conversation_id"], []).append(r)
    con.close()

    turns = []
    for conv_id, msgs in msgs_by_conv.items():
        persona_id = convs.get(conv_id, "primary")
        n = len(msgs)
        for i, m in enumerate(msgs):
            if m["role"] != "user":
                continue
            user_msg = m
            # Look ahead for the agentic assistant reply and the next user msg.
            agentic_reply = None
            next_user_created = None
            j = i + 1
            while j < n:
                nm = msgs[j]
                if nm["role"] == "user":
                    next_user_created = _norm_ts(nm["created_at"])
                    break
                if nm["role"] == "assistant" and agentic_reply is None:
                    routing = json.loads(nm["routing"]) if nm["routing"] else {}
                    if str(routing.get("reasoning", "")).startswith("agentic"):
                        agentic_reply = (nm, routing)
                j += 1
            if agentic_reply is None:
                continue
            reply_row, routing = agentic_reply
            try:
                display_sources = json.loads(reply_row["sources"]) if reply_row["sources"] else []
            except (json.JSONDecodeError, TypeError):
                display_sources = []

            # the two turns immediately preceding this user message
            prev = [(msgs[k]["role"], msgs[k]["content"]) for k in range(max(0, i - 4), i)][-4:]

            next_user_text = None
            if j < n and msgs[j]["role"] == "user":
                next_user_text = msgs[j]["content"]

            pushback = None
            if next_user_text is not None:
                assistant_text = reply_row["content"] or ""
                if _REFUSAL_PATTERNS.search(assistant_text):
                    pushback = bool(_PUSHBACK_PATTERNS.search(next_user_text))

            turns.append({
                "turn_id": reply_row["id"],
                "conversation_id": conv_id,
                "persona": persona_id,
                "user_created_at": _norm_ts(user_msg["created_at"]),
                "reply_created_at": _norm_ts(reply_row["created_at"]),
                "next_user_created_at": next_user_created,
                "message": user_msg["content"],
                "prev_turns": prev,
                "assistant_reply": reply_row["content"],
                "next_user_message": next_user_text,
                "pushback_regex": pushback,
                "tool_calls_total": len(routing.get("sources", [])),
                "tool_names_ordered": routing.get("sources", []),
                "display_sources": display_sources,
                "model_tier": routing.get("reasoning", ""),
            })
    return turns


def join(turns, traces):
    by_conv: dict[str, list[dict]] = {}
    for t in traces:
        by_conv.setdefault(t["conversation_id"], []).append(t)
    for lst in by_conv.values():
        lst.sort(key=lambda t: t["created_at"])

    # chat.py calls start_trace() (line ~866) BEFORE store.add_message() persists
    # the user turn (line ~888), so a trace's created_at is a hair earlier than
    # the user message's created_at it belongs to -- allow a small backward
    # slack to account for that ordering, not clock drift.
    BACKWARD_SLACK = timedelta(seconds=30)

    joined_count = 0
    for turn in turns:
        candidates = by_conv.get(turn["conversation_id"], [])
        q_trunc = turn["message"][:200]
        window_start = _parse_ts(turn["user_created_at"]) - BACKWARD_SLACK
        window_end = _parse_ts(turn["next_user_created_at"]) if turn["next_user_created_at"] else None
        match = None
        for tr in candidates:
            if tr["question"] != q_trunc:
                continue
            tr_ts = _parse_ts(tr["created_at"])
            if tr_ts < window_start:
                continue
            if window_end is not None and tr_ts >= window_end:
                continue
            match = tr
            break
        if match is None:
            turn["joined"] = False
            continue
        turn["joined"] = True
        turn["trace_id"] = match["trace_id"]
        turn["total_ms"] = match["total_ms"]
        rounds, tools_by_round, round_llm_ms = rounds_from_spans(match["spans"])
        turn["rounds"] = rounds
        turn["tools_by_round"] = {str(k): v for k, v in tools_by_round.items()}
        turn["repeated_tool_across_rounds"] = repeated_tool_across_rounds(tools_by_round)
        turn["round1_llm_ms"] = round_llm_ms.get(1)
        joined_count += 1
    return joined_count


def main():
    traces = load_traces()
    turns = load_conversation_turns()
    join(turns, traces)

    dataset_path = OUT_DIR / "e0_dataset.jsonl"
    with dataset_path.open("w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")

    total = len(turns)
    zero_tool = sum(1 for t in turns if t["tool_calls_total"] == 0)
    joined = [t for t in turns if t["joined"]]
    round_dist: dict[int, int] = {}
    three_plus = 0
    three_plus_repeated = 0
    for t in joined:
        r = t["rounds"]
        round_dist[r] = round_dist.get(r, 0) + 1
        if r >= 3:
            three_plus += 1
            if t["repeated_tool_across_rounds"]:
                three_plus_repeated += 1
    hit_cap = sum(1 for t in joined if t["rounds"] >= 5)

    results = {
        "total_agentic_turns": total,
        "zero_tool_turns": zero_tool,
        "zero_tool_pct": round(100 * zero_tool / total, 1) if total else None,
        "joined_to_perf_trace": len(joined),
        "joined_pct": round(100 * len(joined) / total, 1) if total else None,
        "round_distribution": {str(k): v for k, v in sorted(round_dist.items())},
        "turns_3plus_rounds": three_plus,
        "turns_3plus_rounds_with_repeated_tool": three_plus_repeated,
        "turns_hit_5round_cap": hit_cap,
        "issue_baseline_claim": {
            "turns": 870,
            "zero_tool_pct": 55,
            "cap_hit_pct": 4,
            "turns_3plus_rounds": 141,
            "turns_3plus_repeated": 86,
        },
        "note": (
            "issue_baseline_claim is what #1158 asserted; the fields above are "
            "recomputed from the live DBs by this script and may differ because "
            "more turns have accumulated since the issue was filed, and because "
            "routing.tool_rounds (used loosely in the issue's prose) is actually "
            "a tool-CALL count, not a round count -- see this script's docstring."
        ),
    }
    (OUT_DIR / "e0_results.json").write_text(json.dumps(results, indent=2))

    print(f"E0: {total} agentic turns, {len(joined)} joined to a perf trace ({results['joined_pct']}%)")
    print(f"  zero-tool: {zero_tool} ({results['zero_tool_pct']}%)")
    print(f"  round distribution: {results['round_distribution']}")
    print(f"  3+ round turns: {three_plus} (repeated tool: {three_plus_repeated})")
    print(f"  hit 5-round cap: {hit_cap}")
    print(f"  wrote {dataset_path} and {OUT_DIR / 'e0_results.json'}")


if __name__ == "__main__":
    main()
