"""E3 -- tool bundle design: cluster tool co-occurrence into <=6 fixed
bundles, each carrying the 3-tool floor (search_web, search_vault,
person_info) mirroring the existing per-class filter pattern in
api/services/agent_worker/tool_filter.py (cross-cutting tools merged into
every class; see that module's docstring). Fixed bundles are required
because free-form per-turn subsets would bust the prompt cache on every
turn -- this script produces candidate fixed sets, not a per-turn filter.

Method: co-occurrence over ACTUAL tool-using turns from e0_dataset.jsonl
(a turn's "tool set" = the distinct tool names it called, from
routing.sources -- see e0_extract.py's docstring for what that column
does and doesn't capture).

A first pass used scipy hierarchical clustering (Jaccard distance on
per-tool usage vectors, `fcluster(..., criterion="maxclust")`). That
degenerates here: 6 of the 25 tools are never called in this dataset at
all (create/update/delete_calendar_event, send_email_draft,
pause_internet, resume_internet, internet_status -- Google Calendar
writes and eero control never fire from these accumulated chat turns),
so a large block of tool pairs sit at the maximum Jaccard distance
(1.0, "never co-occur") with no distinguishing structure between them.
`linkage()` merges that whole tied block in one step, so `fcluster` can
only produce 1 cluster or 12+ clusters -- never something in between
like 3-6. Switched to a direct greedy set-cover instead, which is what
the bar (coverage) actually asks for and doesn't have this failure
mode: repeatedly add the bundle (floor + one observed turn's exact
non-floor tool set) that covers the most still-uncovered turns, up to 6
bundles. This is the "greedy" option the brief names alongside
hierarchical clustering.

Coverage @ k = % of tool-using turns whose full tool set is a subset of
(floor ∪ one bundle's tools) for at least one bundle, after k greedy
bundles have been added.

Token size per bundle = sum of each included tool's TOOL_DEFINITIONS JSON
size (chars/4, a rough approximation) -- also gives the full 25-tool
schema's total for comparison (~7.3k tokens).

Writes data/jev_eval/e3_results.json. No Jev calls -- this experiment is
pure local analysis over e0_dataset.jsonl.
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from api.services.agent_tools import TOOL_DEFINITIONS  # noqa: E402

DATA_DIR = REPO_ROOT / "data" / "jev_eval"

FLOOR = {"search_web", "search_vault", "person_info"}
ALL_TOOLS = [t["name"] for t in TOOL_DEFINITIONS]
TOOL_TOKENS = {t["name"]: len(json.dumps(t)) / 4 for t in TOOL_DEFINITIONS}


def load_tool_using_turns() -> list[set]:
    turns = []
    with (DATA_DIR / "e0_dataset.jsonl").open() as f:
        for line in f:
            t = json.loads(line)
            names = set(t["tool_names_ordered"])
            if names:
                turns.append(names)
    return turns


def coverage(bundles: list[set], turns: list[set]) -> float:
    covered = 0
    for turn_tools in turns:
        if any(turn_tools <= b for b in bundles):
            covered += 1
    return round(100 * covered / len(turns), 2) if turns else 0.0


def greedy_bundles(turns: list[set], max_bundles: int) -> list[set]:
    """Repeatedly add the bundle (floor + one uncovered turn's exact
    non-floor tool set) that covers the most still-uncovered turns."""
    candidates = {frozenset(t) for t in turns}  # distinct observed tool sets
    covered = [False] * len(turns)
    bundles: list[set] = []
    for _ in range(max_bundles):
        if all(covered):
            break
        best_candidate, best_gain = None, -1
        for cand in candidates:
            bundle = FLOOR | set(cand)
            gain = sum(
                1 for i, t in enumerate(turns)
                if not covered[i] and t <= bundle
            )
            if gain > best_gain:
                best_candidate, best_gain = cand, gain
        if best_gain <= 0:
            break
        bundle = FLOOR | set(best_candidate)
        bundles.append(bundle)
        for i, t in enumerate(turns):
            if t <= bundle:
                covered[i] = True
    return bundles


def main():
    turns = load_tool_using_turns()

    results_by_k = {}
    best = None
    for k in range(1, 7):
        bundles = greedy_bundles(turns, k)
        cov = coverage(bundles, turns)
        bundle_info = [
            {
                "tools": sorted(b),
                "n_tools": len(b),
                "approx_tokens": round(sum(TOOL_TOKENS[t] for t in b)),
            }
            for b in bundles
        ]
        results_by_k[k] = {"n_bundles": len(bundles), "coverage_pct": cov, "bundles": bundle_info}
        if cov >= 90.0 and best is None:
            best = k

    full_schema_tokens = round(sum(TOOL_TOKENS.values()))
    results = {
        "n_tool_using_turns": len(turns),
        "full_schema_approx_tokens": full_schema_tokens,
        "issue_claim_full_schema_tokens": 7300,
        "floor_tools": sorted(FLOOR),
        "by_k": results_by_k,
        "bar": "one bundle covers >=90% of tool-using turns",
        "best_k_meeting_bar": best,
        "verdict": "SHIP" if best is not None else "DROP",
    }
    (DATA_DIR / "e3_results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps({k: v["coverage_pct"] for k, v in results_by_k.items()}, indent=2))
    print(f"full schema ~{full_schema_tokens} tokens (issue claimed ~7300)")
    print(f"best k meeting >=90% coverage bar: {best} -> verdict {results['verdict']}")


if __name__ == "__main__":
    main()
