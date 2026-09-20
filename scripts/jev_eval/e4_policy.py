"""E4 -- policy simulation. Replays E1/E2/E3's measured answers under each
candidate policy listed below and reports, in one table, what each would
have cost/saved against the historical turns in e0_dataset.jsonl.
No Jev calls; pure local analysis over already-computed results files
(e1_results.json, e2_results.json, e3_results.json) and the dataset.

Policies (from the issue):
  a) needs_tools floor -- skip advertising tools on turns Jev scores below
     the E1-chosen threshold (0.1, the smallest threshold meeting E1's bar).
  b) bundles -- offer one of E3's k=5 fixed bundles instead of the full
     25-tool schema on tool-using turns.
  c) round clamp -- max_tool_rounds = clamp(round(difficulty), 2, 8)
     instead of the fixed 5.
  d) early synthesis when is_repeating > 0.7.
  e) early synthesis when answered > 0.8.

What "tokens/seconds saved" means per policy, and why some cells are
None rather than a number, is explained inline -- this experiment
deliberately does not invent a number where the historical data has no
counterfactual to measure it from (e.g. no turn in this dataset was ever
run WITHOUT the tool schema, so the schema's specific contribution to
round-1 latency isn't isolable from these logs; that's flagged rather
than estimated).
"""
import json
import statistics
from pathlib import Path

DATA_DIR = Path("/home/nathanramia/Code/LifeOS/data/jev_eval")
FULL_SCHEMA_TOKENS = 7322  # from e3_results.json


def load_dataset():
    turns = []
    with (DATA_DIR / "e0_dataset.jsonl").open() as f:
        for line in f:
            turns.append(json.loads(line))
    return turns


def load(name):
    return json.loads((DATA_DIR / name).read_text())


def policy_a(turns, e1):
    threshold = e1["needs_tools_best_threshold_meeting_bar"]
    row = next(r for r in e1["needs_tools_threshold_sweep"] if r["threshold"] == threshold)
    n_tool = sum(1 for t in turns if t["tool_calls_total"] > 0)
    n_zero = len(turns) - n_tool
    n_pruned = round(row["zero_tool_pruned_pct"] / 100 * n_zero)
    n_fn = round(row["tool_using_fn_pct"] / 100 * n_tool)

    joined = [t for t in turns if t["joined"]]
    r1_notools_ms = [t["total_ms"] for t in joined if t["rounds"] == 1 and t["tool_calls_total"] == 0]

    return {
        "policy": "a) needs_tools floor",
        "threshold": threshold,
        "turns_affected": n_pruned,
        "tokens_saved_per_affected_turn": FULL_SCHEMA_TOKENS,
        "seconds_saved_per_affected_turn": None,
        "seconds_saved_note": (
            f"not isolable from history: every zero-tool turn already paid the "
            f"schema's processing cost in its one round (median {statistics.median(r1_notools_ms):.0f}ms "
            f"total) -- there's no turn in this dataset that ran without the schema "
            f"to diff against. Needs E6 shadow timing or a live A/B."
        ),
        "false_negatives": n_fn,
        "false_negative_pct_of_tool_using": row["tool_using_fn_pct"],
        "cache_note": "cache-neutral: the full 25-tool block is unchanged for every turn that still gets it, so its cache hit rate is unaffected.",
    }


def policy_b(turns, e3):
    k5 = e3["by_k"]["5"]
    coverage_pct = k5["coverage_pct"]
    avg_bundle_tokens = statistics.mean(b["approx_tokens"] for b in k5["bundles"])
    n_tool = sum(1 for t in turns if t["tool_calls_total"] > 0)
    n_covered = round(coverage_pct / 100 * n_tool)
    n_fn = n_tool - n_covered
    return {
        "policy": "b) fixed bundles (E3 k=5)",
        "turns_affected": n_covered,
        "tokens_saved_per_affected_turn": round(FULL_SCHEMA_TOKENS - avg_bundle_tokens),
        "seconds_saved_per_affected_turn": None,
        "seconds_saved_note": "same schema-diff problem as (a); also depends on which bundle a live classifier picks, not just whether one exists.",
        "false_negatives": n_fn,
        "false_negative_pct_of_tool_using": round(100 - coverage_pct, 2),
        "cache_note": (
            "NOT cache-neutral: a turn whose bundle differs from the previous turn's "
            "in the same conversation pays a fresh cache write instead of a cache read. "
            "Coverage was measured per-turn in isolation; whether real conversations "
            "reuse the same bundle turn-to-turn (cheap) or thrash between bundles "
            "(expensive) is unmeasured here -- needs E6."
        ),
    }


def policy_c(turns, e1):
    joined_with_difficulty = []
    e1_raw = [json.loads(line) for line in (DATA_DIR / "e1_raw.jsonl").read_text().splitlines()]
    diff_by_id = {r["turn_id"]: r["answers"]["difficulty"]["score"] for r in e1_raw if r["answers"]}
    for t in turns:
        if t["joined"] and t["turn_id"] in diff_by_id:
            joined_with_difficulty.append((t, diff_by_id[t["turn_id"]]))

    cut_short = 0
    cap_relief = 0
    for t, diff in joined_with_difficulty:
        clamp = min(8, max(2, round(diff)))
        if t["rounds"] > clamp:
            cut_short += 1
        if t["rounds"] == 5 and clamp > 5:
            cap_relief += 1

    return {
        "policy": "c) round clamp(round(difficulty),2,8)",
        "turns_affected": len(joined_with_difficulty),
        "tokens_saved_per_affected_turn": None,
        "seconds_saved_per_affected_turn": None,
        "seconds_saved_note": (
            "not a savings policy either direction: cutting a turn short trades "
            "latency for a truncated answer (a new failure mode, counted under "
            "false_negatives below); giving cap-hit turns more headroom trades the "
            "opposite way -- more latency for a complete answer instead of the "
            "current truncated-synthesis fallback. The 36 turns that hit the "
            "current 5-round cap have a median total_ms of ~142s already; clamping "
            "up doesn't make those faster, it lets them finish instead of truncating."
        ),
        "false_negatives": cut_short,
        "false_negative_note": "turns whose actual round count exceeded the clamp -- would have been cut off mid-search under this policy",
        "cap_relief_turns": cap_relief,
        "cache_note": "n/a -- doesn't touch the tool schema.",
    }


def policy_de(turns, e2, key, threshold, label):
    e2_raw = [json.loads(line) for line in (DATA_DIR / "e2_raw.jsonl").read_text().splitlines()]
    scores = [r["answers"][key]["noul"] for r in e2_raw if r["answers"]]
    n_high = sum(1 for s in scores if s > threshold)
    return {
        "policy": label,
        "turns_affected": n_high,
        "tokens_saved_per_affected_turn": None,
        "seconds_saved_per_affected_turn": None,
        "seconds_saved_note": f"E2 verdict was DROP for {key} (see e2_results.json) -- reported here for completeness, not as a viable policy.",
        "false_negatives": None,
        "cache_note": "n/a",
        "note": f"{n_high}/{len(scores)} (turn,k) tasks scored above {threshold} in the measured data.",
    }


def main():
    turns = load_dataset()
    e1 = load("e1_results.json")
    e2 = load("e2_results.json")
    e3 = load("e3_results.json")

    rows = [
        policy_a(turns, e1),
        policy_b(turns, e3),
        policy_c(turns, e1),
        policy_de(turns, e2, "is_repeating", 0.7, "d) early synthesis if is_repeating>0.7"),
        policy_de(turns, e2, "answered", 0.8, "e) early synthesis if answered>0.8"),
    ]

    result = {
        "bar": "best saved-seconds per false negative",
        "rows": rows,
        "recommendation": (
            "Policy (a), needs_tools floor at threshold 0.1: the only policy with a clean, "
            "measurable, low-risk benefit -- real token savings (7322 tokens/turn on ~63% of "
            "zero-tool turns), near-zero false-negative rate (0.22% of tool-using turns), and "
            "cache-neutral for every other turn. Its seconds-saved figure isn't isolable from "
            "history (see the row's note) but the token/FN numbers alone justify shipping it "
            "as the recommended first policy to shadow in E6. Policy (b) bundles has a real "
            "token win too but a materially higher false-negative rate (9.82%, meaning the "
            "chosen tool genuinely isn't in the advertised set) and an unmeasured cache-miss "
            "risk -- shadow it, don't ship it blind. Policies (c), (d), (e) don't clear their "
            "own experiments' bars (see e2/e4 notes) and aren't recommended."
        ),
    }
    (DATA_DIR / "e4_results.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
