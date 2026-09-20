"""E2 -- in-loop replay: after each tool round, ask Jev `is_repeating` and
`answered` and score against what actually happened next.

IMPORTANT DATA-AVAILABILITY GAP (read before trusting these numbers):
the brief for this experiment asks for state = {message, (tool, args
summary, result preview <=300 chars) per round}. Tool RESULT content was
never persisted anywhere queryable: `agent_loop.py`'s
`result.tool_calls_log[i]["result_preview"]` lives only in the in-memory
`AgentResult` for the life of one HTTP request; it isn't written to
perf_traces.db (spans only carry name + duration) or conversations.db
(the `sources` display column has tool name + args, truncated 80 chars,
for a hardcoded subset of tools, and drops errored calls entirely; the
`routing.sources` column has tool NAMES only, no args, no results). There
is no historical source of tool result text for this replay. This script
therefore judges `is_repeating` and `answered` from (message, ordered
tool NAME + best-effort ARGS per round) only -- never results. This is a
real handicap for `answered` in particular (whether "the results so far
contain what is needed" is much harder to judge without seeing the
results), so its ship/drop verdict here should be read as a lower bound
on what a result-aware judge could do, not a final answer. See the
report's "what I'd change in the design" section: log a short tool
result preview into the `tool_{name}` perf-trace span's metadata going
forward so a rerun of this experiment doesn't have this gap.

Eligibility: a turn is included only if e0 joined it to a perf trace,
rounds >= 2, AND the tool-name sequence recovered from perf_trace spans
(round-by-round) exactly matches the tool-name sequence recovered from
conversations.db's routing.sources (turn-level, no round boundaries) --
i.e. the two independently-derived orderings agree. This is a real cross-
check, not a formality: they come from different columns of different
tables written at different points in the request.

For each eligible turn, for k = 1 .. rounds-1 (there is no "next round"
to grade against after the last round), ask one Jev call with state =
{message, calls: [(tool, args_summary) for every call in rounds 1..k]}.

Ground truth (see also the module docstring above for why these are
structural proxies, not result-aware):
  - is_repeating@k: True iff round k+1's tool NAMES intersect the tool
    NAMES already called in rounds <=k (exact reproduction of the brief:
    "did round k+1 actually call an identical tool name?").
  - answered@k: True iff no round >k introduces a (tool, args_summary)
    pair not seen in rounds <=k. This is the brief's "added a tool NOT
    seen before" clause; the "or a non-empty result on a new query"
    clause can't be evaluated (no result content), so answered@k is a
    slightly generous proxy -- it can call a turn "answered" when a
    repeated call with the same args actually returned new information
    (e.g. polling a status), which is the same limitation the
    `is_repeating` judgment itself has. Documented, not hidden.

Hand-labeling: 100 sampled (turn, k) pairs are labeled by this script's
operator (Claude, the agent running #1158) for `answered@k`, using the
message, the calls-so-far summary, AND the actual final assistant reply
text pulled from e0_dataset.jsonl's `assistant_reply` field -- reading
the real outcome is the one piece of "result-shaped" signal available
here, even without seeing intermediate tool output. Reasoning is not
saved to the gold file (only the label), per the issue's report
constraints. 30 of the 100 are flagged for Nathan to spot check (turn ids
only).

Writes:
  - data/jev_eval/e2_raw.jsonl    -- one line per (turn_id, k) Jev call
  - data/jev_eval/e2_gold.jsonl   -- 100 hand labels {turn_id, k, label}
  - data/jev_eval/e2_results.json -- aggregate metrics only
"""
import asyncio
import json
import random
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path("/home/nathanramia/Code/LifeOS")
DATA_DIR = REPO_ROOT / "data" / "jev_eval"
CONCURRENCY = 8

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _jev  # noqa: E402

QUESTIONS = {
    "is_repeating": {
        "type": "noul",
        "instructions": (
            "This is an in-progress agent turn: the user's message, then the tools called so far "
            "(name and arguments), grouped by round. If the agent calls another tool right now, "
            "would that next call be repeating a search it has already made in an earlier round "
            "with no new information to justify repeating it?"
        ),
    },
    "answered": {
        "type": "noul",
        "instructions": (
            "This is an in-progress agent turn: the user's message, then the tools called so far "
            "(name and arguments), grouped by round. Based only on which tools have been called "
            "(not their results, which aren't shown here), does it look like the agent has now "
            "gathered everything a careful assistant would need to answer the user's message, "
            "such that stopping and synthesizing an answer now would be reasonable?"
        ),
    },
}


def load_dataset() -> list[dict]:
    turns = []
    with (DATA_DIR / "e0_dataset.jsonl").open() as f:
        for line in f:
            turns.append(json.loads(line))
    return turns


def match_args(tool_names_ordered: list[str], display_sources: list[dict]) -> list[tuple[str, str | None]]:
    """Align the complete, args-free call sequence (routing.sources) with
    the args-carrying but incomplete display `sources` column, greedily
    consuming display entries in order when the next one matches the
    next tool name. See module docstring for why args aren't always
    available."""
    queue = list(display_sources)
    out = []
    for name in tool_names_ordered:
        args = None
        if queue and queue[0].get("file_name", "").startswith(name + "("):
            args = queue.pop(0)["file_name"]
        out.append((name, args))
    return out


def rounds_flat_tool_names(tools_by_round: dict) -> list[str]:
    flat = []
    for k in sorted((int(r) for r in tools_by_round), key=int):
        flat.extend(tools_by_round[str(k)])
    return flat


def eligible_turns(turns: list[dict]) -> list[dict]:
    out = []
    for t in turns:
        if not t.get("joined") or (t.get("rounds") or 0) < 2:
            continue
        flat = rounds_flat_tool_names(t["tools_by_round"])
        if flat != t["tool_names_ordered"]:
            continue  # the two independently-derived orderings disagree -- skip
        out.append(t)
    return out


def build_calls_by_round(turn: dict) -> dict[int, list[tuple[str, str | None]]]:
    aligned = match_args(turn["tool_names_ordered"], turn["display_sources"])
    calls_by_round: dict[int, list[tuple[str, str | None]]] = {}
    idx = 0
    for r in sorted((int(k) for k in turn["tools_by_round"]), key=int):
        names = turn["tools_by_round"][str(r)]
        calls_by_round[r] = aligned[idx: idx + len(names)]
        idx += len(names)
    return calls_by_round


def summarize_calls(calls: list[tuple[str, str | None]]) -> list[str]:
    return [c[1] if c[1] else f"{c[0]}(args unavailable)" for c in calls]


def build_tasks(turns: list[dict]):
    """Return list of {turn_id, k, state, gt_is_repeating, gt_answered}."""
    tasks = []
    for t in turns:
        calls_by_round = build_calls_by_round(t)
        rounds = t["rounds"]
        for k in range(1, rounds):  # no next round to grade after the last one
            calls_so_far = [c for r in range(1, k + 1) for c in calls_by_round.get(r, [])]
            names_so_far = {c[0] for c in calls_so_far}
            args_pairs_so_far = {c for c in calls_so_far}

            next_round_calls = calls_by_round.get(k + 1, [])
            gt_is_repeating = bool({c[0] for c in next_round_calls} & names_so_far)

            later_calls = [c for r in range(k + 1, rounds + 1) for c in calls_by_round.get(r, [])]
            gt_answered = not any(c not in args_pairs_so_far for c in later_calls)

            state = {
                "message": t["message"][:1200],
                "calls_by_round": {
                    str(r): summarize_calls(calls_by_round.get(r, []))
                    for r in range(1, k + 1)
                },
            }
            tasks.append({
                "turn_id": t["turn_id"],
                "k": k,
                "state": state,
                "gt_is_repeating": gt_is_repeating,
                "gt_answered": gt_answered,
            })
    return tasks


async def run_all(tasks: list[dict]) -> list[dict]:
    sem = asyncio.Semaphore(CONCURRENCY)
    results = [None] * len(tasks)

    async def one(i, task):
        async with sem:
            try:
                answers = await _jev.ask(task["state"], QUESTIONS)
                results[i] = {"turn_id": task["turn_id"], "k": task["k"], "answers": answers, "error": None}
            except Exception as e:
                results[i] = {"turn_id": task["turn_id"], "k": task["k"], "answers": None, "error": str(e)}

    await asyncio.gather(*(one(i, t) for i, t in enumerate(tasks)))
    return results


def sample_for_review(tasks: list[dict], turns_by_id: dict, n=100, seed=1158) -> list[dict]:
    """Sample n (turn, k) pairs and dump the material an operator needs to
    hand-label `answered@k` -- message, calls made through round k, and the
    turn's actual final assistant reply (the only "what really happened"
    signal available without tool result content). Written to
    e2_gold_review.jsonl for the operator to read and label; this function
    does not itself produce labels."""
    rng = random.Random(seed)
    sample = rng.sample(tasks, min(n, len(tasks)))
    review = []
    for task in sample:
        turn = turns_by_id[task["turn_id"]]
        review.append({
            "turn_id": task["turn_id"],
            "k": task["k"],
            "message": turn["message"][:400],
            "calls_by_round": task["state"]["calls_by_round"],
            "rounds_total": turn["rounds"],
            "assistant_reply": (turn.get("assistant_reply") or "")[:600],
        })
    return review


def compute_metrics(tasks: list[dict], raw: list[dict], gold: list[dict]) -> dict:
    by_key = {(r["turn_id"], r["k"]): r for r in raw if r["answers"] is not None}
    errors = sum(1 for r in raw if r["answers"] is None)

    rep_true, rep_pred = [], []
    ans_true, ans_pred = [], []
    for t in tasks:
        r = by_key.get((t["turn_id"], t["k"]))
        if r is None:
            continue
        rep_true.append(1 if t["gt_is_repeating"] else 0)
        rep_pred.append(r["answers"]["is_repeating"]["noul"])
        ans_true.append(1 if t["gt_answered"] else 0)
        ans_pred.append(r["answers"]["answered"]["noul"])

    rep_true = np.array(rep_true)
    rep_pred_bin = (np.array(rep_pred) >= 0.5).astype(int)
    tp = int(((rep_pred_bin == 1) & (rep_true == 1)).sum())
    fp = int(((rep_pred_bin == 1) & (rep_true == 0)).sum())
    fn = int(((rep_pred_bin == 0) & (rep_true == 1)).sum())
    rep_precision = round(tp / (tp + fp), 3) if (tp + fp) else None
    rep_recall = round(tp / (tp + fn), 3) if (tp + fn) else None

    # "early-synthesis when answered>0.8 would not have cut a turn that later
    # found new information more than 5% of the time" -- i.e. among turns
    # where Jev's answered score > 0.8, what fraction have gt_answered False
    # (structurally: something new happened later)?
    ans_pred_arr = np.array(ans_pred)
    ans_true_arr = np.array(ans_true)
    high_conf = ans_pred_arr > 0.8
    n_high_conf = int(high_conf.sum())
    wrongly_cut = int(((high_conf) & (ans_true_arr == 0)).sum())
    wrongly_cut_pct = round(100 * wrongly_cut / n_high_conf, 2) if n_high_conf else None

    gold_labels = np.array([g["label"] for g in gold])
    gold_pred = []
    for g in gold:
        r = by_key.get((g["turn_id"], g["k"]))
        gold_pred.append(r["answers"]["answered"]["noul"] if r else None)
    valid = [(gl, gp) for gl, gp in zip(gold_labels.tolist(), gold_pred) if gp is not None]
    gold_agree = None
    if valid:
        gold_agree = round(sum(1 for gl, gp in valid if (gp >= 0.5) == bool(gl)) / len(valid), 3)

    return {
        "n_eligible_turns": len({t["turn_id"] for t in tasks}),
        "n_round_tasks": len(tasks),
        "n_errors": errors,
        "is_repeating_precision_at_0.5": rep_precision,
        "is_repeating_recall_at_0.5": rep_recall,
        "is_repeating_bar": ">=90% precision",
        "is_repeating_verdict": "SHIP" if (rep_precision or 0) >= 0.90 else "DROP",
        "answered_n_high_confidence(>0.8)": n_high_conf,
        "answered_wrongly_cut_pct": wrongly_cut_pct,
        "answered_bar": "early-synthesis at answered>0.8 wrongly cuts a turn with more info later <=5% of the time",
        "answered_verdict": "SHIP" if (wrongly_cut_pct is not None and wrongly_cut_pct <= 5.0) else "DROP",
        "answered_gold_sample_agreement": gold_agree,
        "answered_gold_sample_n": len(valid),
        "data_gap_caveat": (
            "is_repeating and answered were judged (both by Jev and by the operator hand "
            "labels) from tool name + args only -- no tool RESULT content exists in any "
            "persisted store for historical turns. See this script's module docstring."
        ),
    }


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-turns", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    turns = load_dataset()
    turns_by_id = {t["turn_id"]: t for t in turns}
    elig = eligible_turns(turns)
    print(f"E2: {len(elig)}/{len(turns)} turns eligible (joined, rounds>=2, span/routing tool-order agree)")
    if args.limit_turns:
        elig = elig[: args.limit_turns]

    tasks = build_tasks(elig)
    print(f"E2: {len(tasks)} (turn, k) round tasks")

    raw_path = DATA_DIR / "e2_raw.jsonl"
    if raw_path.exists() and not args.force:
        print(f"Reusing existing {raw_path} (delete it or pass --force to re-query Jev)")
        raw = [json.loads(line) for line in raw_path.read_text().splitlines()]
    else:
        raw = asyncio.run(run_all(tasks))
        with raw_path.open("w") as f:
            for r in raw:
                f.write(json.dumps(r) + "\n")

    gold_path = DATA_DIR / "e2_gold.jsonl"
    review_path = DATA_DIR / "e2_gold_review.jsonl"
    if gold_path.exists() and not args.force:
        gold = [json.loads(line) for line in gold_path.read_text().splitlines()]
        spot_check_ids = sorted({g["turn_id"] for g in gold})[:30]
        (DATA_DIR / "e2_spot_check_turn_ids.json").write_text(json.dumps(spot_check_ids, indent=2))
        metrics = compute_metrics(tasks, raw, gold)
        (DATA_DIR / "e2_results.json").write_text(json.dumps(metrics, indent=2))
        print(json.dumps(metrics, indent=2))
        print(_jev.usage_summary())
        return

    if not review_path.exists() or args.force:
        review = sample_for_review(tasks, turns_by_id)
        with review_path.open("w") as f:
            for r in review:
                f.write(json.dumps(r) + "\n")
        print(f"Wrote {review_path} -- read it and write {gold_path} "
              f"(one JSON object per line: {{\"turn_id\":..., \"k\":..., \"label\": true|false}}), "
              f"then re-run this script to compute metrics.")
        return

    print(f"{review_path} exists but {gold_path} does not yet -- label it, then re-run.")


if __name__ == "__main__":
    main()
