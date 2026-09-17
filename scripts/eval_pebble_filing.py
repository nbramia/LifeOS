#!/usr/bin/env python3
"""
Score the configured Pebble filing classifier's task-vs-log judgment.

Pebble's filing decision (log-only by default; a task only when the speaker
actively asks for one) lives entirely in the prompt and the model
(`api/services/journal_filing_policy.py`, `api.services.pebble_capture.
PebbleJournalClassifier`) -- application code proves execution authority for
delegated tasks and agent schedules, but never decides whether a plain task
files. This script is how that judgment gets re-checked, e.g. after a model
swap: it is a standalone tool, not a test-suite gate, because it makes a real
network call to whichever provider is configured (the remote provider when
`settings.remote_llm_configured`, else the local llama-server) and costs
money or time.

Usage:
    ~/.venvs/lifeos/bin/python scripts/eval_pebble_filing.py

Exits non-zero if the score falls below PASS_THRESHOLD (see below).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.pebble_capture import PebbleJournalClassifier, validate_plan  # noqa: E402

# Chosen so an occasional miss on a genuinely ambiguous case doesn't fail a
# healthy model, while still catching a model that has lost the log-only
# default. Revisit this number, not the case table, if a new model's honest
# score settles below it.
PASS_THRESHOLD = 0.8

_RECORDED_AT = "2030-01-01T10:00:00Z"

# (transcript, expect_task) -- synthetic, mirrors real Pebble capture grammar.
_CASES = [
    ("Add a task to take the synthetic dog outside.", True),
    ("Remind me to call the plumber.", True),
    ("Put the oil change on my list.", True),
    ("Assign the task related to the synthetic report to me.", True),
    ("Add a task assigned to #claude, to review the synthetic report.", True),
    ("Take the synthetic dog outside.", False),
    ("Test take out the trash.", False),
    ("I should probably call the plumber.", False),
    ("The blue mug is on the table.", False),
    ("I have a doctor's appointment tomorrow and the car needs an oil change.", False),
    ("Let me know how the synthetic report turned out.", False),
]

# (transcript, expected single filed item) -- only the first-asked item
# should file; the rest is thinking aloud.
_MULTI_ITEM_CASES = [
    (
        "add a task to buy milk and feed the cat before dinner and water the garden plants",
        "buy milk",
    ),
    ("remind me to check the mail, buy milk, walk the dog", "check the mail"),
]

# ---------------------------------------------------------------------------
# Held-out set. These cases must never appear in JOURNAL_BEHAVIOR_CASES (the
# few-shot examples injected into the classifier prompt) or be added above --
# the cases above overlap heavily with those examples, so a model can score
# well there partly by repeating its own examples back. This section is what
# actually falsifies "the model still has the log-only default": phrasings it
# has never seen in the prompt or in this file's other tables. If a future
# edit is tempted to "helpfully" promote one of these into
# JOURNAL_BEHAVIOR_CASES because it's a good example, don't -- that would
# quietly turn this section back into more of the same overlap it exists to
# avoid.

# (transcript, expect_task)
_HELD_OUT_CASES = [
    ("Can you add a task to book the synthetic dentist?", True),
    ("Throw a to-do on there for renewing the synthetic passport.", True),
    ("Remind me to email the synthetic landlord about the lease.", True),
    ("Put calling the synthetic bank on my list.", True),
    ("I keep meaning to sort out the synthetic garage.", False),
    ("The synthetic delivery came while I was out.", False),
    ("We were talking about maybe redoing the synthetic bathroom next year.", False),
    ("I need to call the synthetic vet at some point.", False),
    ("Note to self, the synthetic router keeps dropping.", False),
    ("Honestly the synthetic gutters are a mess again.", False),
]

# (transcript, expected single filed item)
_HELD_OUT_MULTI_ITEM_CASES = [
    (
        "add a task to renew the synthetic passport and I also want to think about the "
        "synthetic garage",
        "renew the synthetic passport",
    ),
]

# transcripts that must file as a notify schedule, not a plain task
_HELD_OUT_SCHEDULE_CASES = [
    "Remind me at 4 PM tomorrow to take out the synthetic bins.",
]


def _task_actions(filed):
    return [action for action in filed if action.kind == "task"]


async def _score_case(classifier, transcript, expect_task):
    try:
        raw = await classifier.classify(transcript, _RECORDED_AT)
        filed = validate_plan(raw, transcript=transcript, recorded_at=_RECORDED_AT)
    except Exception as exc:  # noqa: BLE001 - report, never crash the run
        return False, f"error: {exc!r}"
    tasks = _task_actions(filed)
    got_task = len(tasks) >= 1
    passed = got_task is expect_task
    label = "task" if got_task else "log-only"
    return passed, f"got {label} ({len(tasks)} task action(s))"


async def _score_multi_item_case(classifier, transcript, expected_phrase):
    try:
        raw = await classifier.classify(transcript, _RECORDED_AT)
        filed = validate_plan(raw, transcript=transcript, recorded_at=_RECORDED_AT)
    except Exception as exc:  # noqa: BLE001
        return False, f"error: {exc!r}"
    tasks = _task_actions(filed)
    if len(tasks) != 1:
        return False, f"expected exactly 1 task, got {len(tasks)}"
    evidence = (tasks[0].action_evidence or tasks[0].title).casefold()
    if expected_phrase.casefold() not in evidence:
        return False, f"filed {evidence!r}, expected it to contain {expected_phrase!r}"
    return True, f"filed {evidence!r}"


async def _score_schedule_case(classifier, transcript):
    try:
        raw = await classifier.classify(transcript, _RECORDED_AT)
        filed = validate_plan(raw, transcript=transcript, recorded_at=_RECORDED_AT)
    except Exception as exc:  # noqa: BLE001 - report, never crash the run
        return False, f"error: {exc!r}"
    schedules = [a for a in filed if a.kind == "schedule" and a.action == "notify"]
    tasks = _task_actions(filed)
    passed = len(schedules) == 1 and len(tasks) == 0
    return passed, f"got {len(schedules)} notify schedule(s), {len(tasks)} task(s)"


async def _run_cases(classifier):
    """Score the original table (overlaps the prompt's few-shot examples)."""
    results = []
    for transcript, expect_task in _CASES:
        passed, detail = await _score_case(classifier, transcript, expect_task)
        want = "TASK" if expect_task else "LOG"
        results.append((passed, want, transcript, detail))
    for transcript, expected_phrase in _MULTI_ITEM_CASES:
        passed, detail = await _score_multi_item_case(classifier, transcript, expected_phrase)
        results.append((passed, "MULTI", transcript, detail))
    return results


async def _run_held_out_cases(classifier):
    """Score the held-out set -- absent from the prompt and the table above."""
    results = []
    for transcript, expect_task in _HELD_OUT_CASES:
        passed, detail = await _score_case(classifier, transcript, expect_task)
        want = "TASK" if expect_task else "LOG"
        results.append((passed, want, transcript, detail))
    for transcript, expected_phrase in _HELD_OUT_MULTI_ITEM_CASES:
        passed, detail = await _score_multi_item_case(classifier, transcript, expected_phrase)
        results.append((passed, "MULTI", transcript, detail))
    for transcript in _HELD_OUT_SCHEDULE_CASES:
        passed, detail = await _score_schedule_case(classifier, transcript)
        results.append((passed, "SCHED", transcript, detail))
    return results


def _print_section(title, results):
    print(f"\n== {title} ==")
    width = max(len(transcript) for _, _, transcript, _ in results)
    for passed, want, transcript, detail in results:
        mark = "PASS" if passed else "FAIL"
        print(f"{mark}  {want:<5} {transcript:<{width}}  {detail}")
    correct = sum(1 for passed, *_ in results if passed)
    total = len(results)
    print(f"{correct}/{total} correct ({correct / total:.0%})")
    return correct, total


async def main() -> int:
    classifier = PebbleJournalClassifier()

    table_results = await _run_cases(classifier)
    held_out_results = await _run_held_out_cases(classifier)

    table_correct, table_total = _print_section("Table (overlaps prompt examples)", table_results)
    held_out_correct, held_out_total = _print_section("Held-out (absent from prompt)", held_out_results)

    total = table_total + held_out_total
    correct = table_correct + held_out_correct
    score = correct / total
    print(f"\nOverall: {correct}/{total} correct ({score:.0%}); threshold {PASS_THRESHOLD:.0%}")

    if score < PASS_THRESHOLD:
        print("FAILED: score is below threshold")
        return 1
    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
