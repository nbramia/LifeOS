#!/usr/bin/env python3
"""Run the execution question set against whatever is serving :8080.

Exercises the REAL agentic path (`agent_loop.run_agent_loop`), so results
reflect the tool catalog in `agent_tools.TOOL_DEFINITIONS` — NOT the MCP
schema. If you changed MCP tool definitions, this harness will not see it.

One model resident at a time. Never run two of these concurrently: bandwidth
contention on a unified-memory box produces artifacts that look like model
failures.

Usage: qrun.py <label> <off|low>
  off  -> enable_thinking=False   (router-style; classification)
  low  -> reasoning_effort="low"  (agentic; enough to terminate cleanly)

Env: OUT_DIR (default cwd), LIFEOS_ROOT (default ~/Code/LifeOS)
"""
import asyncio
import json
import os
import sys
import time

ROOT = os.environ.get("LIFEOS_ROOT", os.path.expanduser("~/Code/LifeOS"))
sys.path.insert(0, ROOT)

LABEL = sys.argv[1] if len(sys.argv) > 1 else "candidate"
MODE = sys.argv[2] if len(sys.argv) > 2 else "low"
OUT_DIR = os.environ.get("OUT_DIR", ".")
OUT = os.path.join(OUT_DIR, f"q_{LABEL}.json")

from api.services import llm_client as llm_mod  # noqa: E402

# Per-request reasoning control, applied without touching .env (which is a
# Syncthing symlink and must not be edited for an experiment).
_orig = llm_mod.LocalLLMClient.astream


def _patched(self, *a, **kw):
    if MODE == "low":
        kw["reasoning_effort"] = "low"
        kw.pop("enable_thinking", None)
    else:
        kw["enable_thinking"] = False
        kw.pop("reasoning_effort", None)
    return _orig(self, *a, **kw)


llm_mod.LocalLLMClient.astream = _patched

from api.services.agent_loop import run_agent_loop  # noqa: E402

# Multi-step questions that require driving several tools and synthesising.
# Keep them stable across rounds — changing the set invalidates comparison
# with every previous run.
QUESTIONS = [
    "Look at my open tasks and my calendar this week and tell me whether the week is realistically achievable.",
    "What did I say I would follow up on recently, and is there any sign I actually did it?",
    "Which of my open tasks are overdue, and which should I prioritize today?",
    "Find anything from the last two weeks that looks like it's waiting on a reply from me.",
    "Summarize what I've been working on this week based on my notes and tasks.",
]


async def main():
    rows = []
    print(f"  === {LABEL} (mode={MODE}) ===", flush=True)
    for q in QUESTIONS:
        t0 = time.time()
        txt = ""
        err = None
        try:
            async for ev in run_agent_loop(q, force_local=True, max_tool_rounds=5):
                if ev.get("type") == "text":
                    txt += ev.get("content") or ""
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            txt = f"[ERROR] {err}"
        el = round(time.time() - t0, 1)
        rows.append({"q": q, "text": txt, "elapsed": el, "chars": len(txt), "error": err})
        json.dump(rows, open(OUT, "w"), indent=2)
        # A 400 here almost always means the context window is too small for the
        # accumulated tool results — the run is void, not the model bad.
        flag = "  <-- CHECK CONTEXT" if err and "400" in str(err) else ""
        print(f"  {el:7.1f}s {len(txt):5d} ch  {q[:46]}{flag}", flush=True)
    print(f"wrote {OUT}")
    if any(r["error"] for r in rows):
        print("!! errors present — do NOT score this run until resolved")


asyncio.run(main())
