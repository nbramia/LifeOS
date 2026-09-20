"""E5 -- Jev call latency, and whether it arrives while the first tool
round is still running if wired concurrently (the constraint being
measured against: the shadow call runs alongside that round, not ahead
of it).

Two measurements:
  1. 500 SEQUENTIAL Jev calls (no concurrency -- isolates per-call latency,
     not queueing) using the E1 pre-turn question set against a realistic,
     large real turn's state (a morning-briefing turn; picked because it's
     one of the bigger real states in e0_dataset.jsonl, close to the
     issue's "~1.5k tokens" sizing note) -> p50/p95/p99.
  2. A bootstrap race: for 100 trials, sample one of the 500 measured Jev
     latencies and one round-1 LLM-call latency from e0_dataset.jsonl's
     `round1_llm_ms` (real historical round-1 durations, joined turns
     only -- see e0_extract.py's docstring) and check which is smaller.
     This reuses the SAME 500 measured latencies rather than making new
     Jev calls (issue budget) -- "timing concurrently" is simulated by
     racing the two measured distributions, not by re-running both
     systems live side by side, which is what E6's production shadow is
     for.

Writes data/jev_eval/e5_results.json.
"""
import asyncio
import json
import random
import sys
from pathlib import Path

DATA_DIR = Path("/home/nathanramia/Code/LifeOS/data/jev_eval")
N_SEQUENTIAL = 500
N_RACE_TRIALS = 100

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _jev  # noqa: E402

# Reuse E1's exact question set so this measures the real pre-turn call shape.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from e1_preturn import QUESTIONS, build_state  # noqa: E402


def pick_large_state() -> dict:
    turns = []
    with (DATA_DIR / "e0_dataset.jsonl").open() as f:
        for line in f:
            turns.append(json.loads(line))
    # Largest message + most prev-turn context among real turns, as a
    # realistic upper-bound state size.
    turns.sort(key=lambda t: len(t["message"]) + sum(len(c or "") for _, c in t["prev_turns"]), reverse=True)
    return build_state(turns[0])


async def run_sequential(state: dict, n: int) -> list[float]:
    latencies = []
    async with __import__("httpx").AsyncClient(timeout=30) as client:
        for _ in range(n):
            t0 = asyncio.get_event_loop().time()
            await _jev.ask(state, QUESTIONS, client=client)
            latencies.append((asyncio.get_event_loop().time() - t0) * 1000)
    return latencies


def load_round1_ms() -> list[float]:
    vals = []
    with (DATA_DIR / "e0_dataset.jsonl").open() as f:
        for line in f:
            t = json.loads(line)
            if t.get("joined") and t.get("round1_llm_ms"):
                vals.append(t["round1_llm_ms"])
    return vals


def pct(vals: list[float], p: float) -> float:
    s = sorted(vals)
    idx = min(len(s) - 1, int(len(s) * p))
    return s[idx]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=N_SEQUENTIAL)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    raw_path = DATA_DIR / "e5_raw_latencies.json"
    if raw_path.exists() and not args.force:
        print(f"Reusing existing {raw_path} (delete it or pass --force to re-measure)")
        latencies = json.loads(raw_path.read_text())
    else:
        state = pick_large_state()
        state_tokens_approx = len(json.dumps(state)) / 4
        print(f"State size ~{state_tokens_approx:.0f} tokens (approx, chars/4). Running {args.n} sequential Jev calls...")
        latencies = asyncio.run(run_sequential(state, args.n))
        raw_path.write_text(json.dumps(latencies))

    round1_ms = load_round1_ms()
    rng = random.Random(1158)
    wins = 0
    for _ in range(N_RACE_TRIALS):
        jev_ms = rng.choice(latencies)
        r1_ms = rng.choice(round1_ms)
        if jev_ms < r1_ms:
            wins += 1
    share_arrived_first = round(100 * wins / N_RACE_TRIALS, 1)

    result = {
        "n_calls": len(latencies),
        "p50_ms": round(pct(latencies, 0.50), 1),
        "p95_ms": round(pct(latencies, 0.95), 1),
        "p99_ms": round(pct(latencies, 0.99), 1),
        "max_ms": round(max(latencies), 1),
        "n_race_trials": N_RACE_TRIALS,
        "round1_llm_ms_source_n": len(round1_ms),
        "share_jev_arrives_before_round1_pct": share_arrived_first,
        "bar": "speculative wiring if >=95% arrive first; else serial only for judgments E4 selected",
        "verdict": "SPECULATIVE (run alongside round 1)" if share_arrived_first >= 95.0 else "SERIAL ONLY (gate before advertising tools, don't race)",
    }
    (DATA_DIR / "e5_results.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    print(_jev.usage_summary())


if __name__ == "__main__":
    main()
