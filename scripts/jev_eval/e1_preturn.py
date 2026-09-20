"""E1 -- replay the pre-turn Jev question set over every turn in e0_dataset.jsonl
and score the answers against what the orchestrator actually did.

Questions asked per turn (see QUESTIONS below): `needs_tools` (noul), nine
tool-family nouls, `difficulty` (score, 5 levels = number of distinct data
sources), `is_followup` (noul). State = {persona, last two turns, message}.

Writes:
  - data/jev_eval/e1_raw.jsonl    -- one Jev answer set per turn (has message
    text via turn_id join back to e0_dataset.jsonl; not committed)
  - data/jev_eval/e1_results.json -- aggregate metrics only (safe to quote)

Ground truth, all derived from e0_dataset.jsonl (see its docstring for the
data-availability caveats this inherits):
  - needs_tools:  tool_calls_total > 0
  - family used:  TOOL_TO_FAMILY maps each tool_names_ordered entry to one
    of the 9 families; a family counts as "used" if any tool in it was
    called during the turn. search_drive has no listed family in the issue
    (the 9 named families are comm/calendar/people-crm/vault/finance/
    tasks/fitness/web/home) -- bucketed under `vault` here as the closest
    fit (documents/knowledge store); see README.md.
  - difficulty:   compared to `rounds` (joined turns only -- rounds needs
    perf_trace span data)
  - is_followup:  the PRODUCTION function api.services.chat_helpers.
    expand_followup_query, called with the same last-2-turn history this
    script sends to Jev. This is the primary expansion path only; chat.py
    also has a secondary `conversation_context`-based expansion path for
    person-reference follow-ups that this does not reproduce (documented
    gap, not exercised by expand_followup_query's substring test).
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from scipy.stats import spearmanr
from sklearn.metrics import roc_auc_score

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from api.services.chat_helpers import expand_followup_query  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _jev  # noqa: E402

DATA_DIR = REPO_ROOT / "data" / "jev_eval"
CONCURRENCY = 8

FAMILIES = {
    "comm": ["search_email", "search_slack", "get_message_history", "create_email_draft", "send_email_draft"],
    "calendar": ["search_calendar", "create_calendar_event", "update_calendar_event", "delete_calendar_event"],
    "people_crm": ["person_info"],
    "vault": ["search_vault", "read_vault_file", "save_memory", "search_memories", "search_drive"],
    "finance": ["search_finances"],
    "tasks": ["manage_tasks", "manage_reminders", "manage_schedules", "manage_human_queue"],
    "fitness": ["manage_workouts"],
    "web": ["search_web"],
    "home": ["pause_internet", "resume_internet", "internet_status"],
}
TOOL_TO_FAMILY = {tool: fam for fam, tools in FAMILIES.items() for tool in tools}

FAMILY_INSTRUCTIONS = {
    "comm": "Would answering this message require searching or sending email, Slack messages, or iMessage/text messages?",
    "calendar": "Would answering this message require checking, creating, or modifying a calendar event?",
    "people_crm": "Would answering this message require looking up a specific person's profile, facts, or relationship/contact history?",
    "vault": "Would answering this message require searching notes, journal entries, saved memories, or files (vault or Drive)?",
    "finance": "Would answering this message require looking up financial data (transactions, budgets, accounts, or investments)?",
    "tasks": "Would answering this message require creating, listing, or updating a task, reminder, or scheduled automation?",
    "fitness": "Would answering this message require looking up or logging a workout, fitness metric, or exercise history?",
    "web": "Would answering this message require searching the public web for current information not in personal data?",
    "home": "Would answering this message require checking or changing home network/internet access (e.g. pausing or resuming a device)?",
}

QUESTIONS = {
    "needs_tools": {
        "type": "noul",
        "instructions": (
            "This is one turn of a chat with a personal-assistant agent that has tools for "
            "search, calendar, email, tasks, etc. Does answering THIS message require calling "
            "at least one tool, rather than being answerable from general knowledge or the "
            "conversation history alone?"
        ),
    },
    **{
        f"family_{fam}": {"type": "noul", "instructions": instr}
        for fam, instr in FAMILY_INSTRUCTIONS.items()
    },
    "difficulty": {
        "type": "score",
        "instructions": "How many distinct personal-data sources would a careful assistant need to consult to fully answer this message?",
        "criteria": ["0 sources", "1 source", "2 sources", "3 sources", "4 or more sources"],
    },
    "is_followup": {
        "type": "noul",
        "instructions": (
            "Is this message a follow-up that can only be understood using the prior "
            "conversation turns (e.g. a pronoun or implicit reference to something just "
            "discussed), rather than a self-contained question?"
        ),
    },
}


def build_state(turn: dict) -> dict:
    prev = turn["prev_turns"][-2:]
    return {
        "persona": turn["persona"],
        "prev_turns": [{"role": r, "content": (c or "")[:600]} for r, c in prev],
        "message": turn["message"][:1200],
    }


def load_dataset() -> list[dict]:
    turns = []
    with (DATA_DIR / "e0_dataset.jsonl").open() as f:
        for line in f:
            turns.append(json.loads(line))
    return turns


async def run_all(turns: list[dict]) -> list[dict]:
    sem = asyncio.Semaphore(CONCURRENCY)
    results = [None] * len(turns)

    async def one(i, turn):
        async with sem:
            try:
                answers = await _jev.ask(build_state(turn), QUESTIONS)
                results[i] = {"turn_id": turn["turn_id"], "answers": answers, "error": None}
            except Exception as e:
                results[i] = {"turn_id": turn["turn_id"], "answers": None, "error": str(e)}

    await asyncio.gather(*(one(i, t) for i, t in enumerate(turns)))
    return results


def local_is_followup(turn: dict) -> bool:
    prev = turn["prev_turns"][-2:]
    hist = [SimpleNamespace(role=r, content=c or "") for r, c in prev]
    expanded = expand_followup_query(turn["message"], hist)
    return expanded != turn["message"]


def ground_truth_families(turn: dict) -> set:
    used = set()
    for name in turn["tool_names_ordered"]:
        fam = TOOL_TO_FAMILY.get(name)
        if fam:
            used.add(fam)
    return used


def compute_metrics(turns: list[dict], raw: list[dict]) -> dict:
    by_id = {r["turn_id"]: r for r in raw if r["answers"] is not None}
    errors = sum(1 for r in raw if r["answers"] is None)

    y_true, y_score = [], []
    fam_true = {fam: [] for fam in FAMILIES}
    fam_score = {fam: [] for fam in FAMILIES}
    difficulty_scores, rounds_gt = [], []
    followup_local, followup_jev = [], []

    for t in turns:
        r = by_id.get(t["turn_id"])
        if r is None:
            continue
        a = r["answers"]
        y_true.append(1 if t["tool_calls_total"] > 0 else 0)
        y_score.append(a["needs_tools"]["noul"])

        used_fams = ground_truth_families(t)
        for fam in FAMILIES:
            fam_true[fam].append(1 if fam in used_fams else 0)
            fam_score[fam].append(a[f"family_{fam}"]["noul"])

        if t.get("joined") and t.get("rounds") is not None:
            difficulty_scores.append(a["difficulty"]["score"])
            rounds_gt.append(t["rounds"])

        followup_local.append(1 if local_is_followup(t) else 0)
        followup_jev.append(a["is_followup"]["noul"])

    y_true = np.array(y_true)
    y_score = np.array(y_score)

    auc = roc_auc_score(y_true, y_score) if len(set(y_true.tolist())) > 1 else None

    n_zero = int((y_true == 0).sum())
    n_tool = int((y_true == 1).sum())
    threshold_table = []
    best_threshold_meeting_bar = None
    for thresh in [round(0.05 * i, 2) for i in range(1, 11)]:
        pred_needs = y_score >= thresh
        # "prune" = predicted NOT needing tools -> would skip advertising the
        # full tool schema this turn.
        pruned_zero = int(((~pred_needs) & (y_true == 0)).sum())
        fn = int(((~pred_needs) & (y_true == 1)).sum())
        pruned_pct = round(100 * pruned_zero / n_zero, 1) if n_zero else 0.0
        fn_pct = round(100 * fn / n_tool, 2) if n_tool else 0.0
        row = {"threshold": thresh, "zero_tool_pruned_pct": pruned_pct, "tool_using_fn_pct": fn_pct}
        threshold_table.append(row)
        if pruned_pct >= 40.0 and fn_pct < 2.0 and best_threshold_meeting_bar is None:
            best_threshold_meeting_bar = thresh

    family_metrics = {}
    for fam in FAMILIES:
        t_arr = np.array(fam_true[fam])
        s_arr = np.array(fam_score[fam])
        pred = s_arr >= 0.5
        tp = int((pred & (t_arr == 1)).sum())
        fp = int((pred & (t_arr == 0)).sum())
        fn = int((~pred & (t_arr == 1)).sum())
        precision = round(tp / (tp + fp), 3) if (tp + fp) else None
        recall = round(tp / (tp + fn), 3) if (tp + fn) else None
        family_metrics[fam] = {
            "n_positive": int(t_arr.sum()),
            "precision_at_0.5": precision,
            "recall_at_0.5": recall,
        }

    spearman_rho, spearman_p = (None, None)
    if len(difficulty_scores) > 2:
        spearman_rho, spearman_p = spearmanr(difficulty_scores, rounds_gt)

    fl = np.array(followup_local)
    fj_bin = (np.array(followup_jev) >= 0.5).astype(int)
    followup_agree = float((fl == fj_bin).mean()) if len(fl) else None
    followup_auc = roc_auc_score(fl, followup_jev) if len(set(fl.tolist())) > 1 else None

    return {
        "n_turns_total": len(turns),
        "n_turns_answered": len(y_true),
        "n_errors": errors,
        "needs_tools_auc": None if auc is None else round(float(auc), 3),
        "needs_tools_threshold_sweep": threshold_table,
        "needs_tools_best_threshold_meeting_bar": best_threshold_meeting_bar,
        "needs_tools_bar": "some threshold prunes >=40% of zero-tool turns with <2% FN rate on tool-using turns",
        "needs_tools_verdict": "SHIP" if best_threshold_meeting_bar is not None else "DROP",
        "family_metrics": family_metrics,
        "difficulty_vs_rounds_spearman_rho": None if spearman_rho is None else round(float(spearman_rho), 3),
        "difficulty_vs_rounds_spearman_p": None if spearman_p is None else round(float(spearman_p), 4),
        "difficulty_vs_rounds_n": len(difficulty_scores),
        "is_followup_agreement_with_local_regex_at_0.5": None if followup_agree is None else round(followup_agree, 3),
        "is_followup_auc_vs_local_regex": None if followup_auc is None else round(float(followup_auc), 3),
        "is_followup_n_local_positive": int(fl.sum()),
    }


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="only process the first N turns (smoke test)")
    ap.add_argument("--force", action="store_true", help="re-query Jev even if e1_raw.jsonl exists")
    args = ap.parse_args()

    turns = load_dataset()
    if args.limit:
        turns = turns[: args.limit]
    raw_path = DATA_DIR / "e1_raw.jsonl"
    if raw_path.exists() and not args.force:
        print(f"Reusing existing {raw_path} (delete it or pass --force to re-query Jev)")
        raw = [json.loads(line) for line in raw_path.read_text().splitlines()]
    else:
        raw = asyncio.run(run_all(turns))
        with raw_path.open("w") as f:
            for r in raw:
                f.write(json.dumps(r) + "\n")

    metrics = compute_metrics(turns, raw)
    (DATA_DIR / "e1_results.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))
    print(_jev.usage_summary())


if __name__ == "__main__":
    main()
