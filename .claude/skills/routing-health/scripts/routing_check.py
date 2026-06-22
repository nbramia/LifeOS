#!/usr/bin/env python3
"""Routing-health checker for the LifeOS chat router.

Verifies that every chat configuration routes correctly:
  surfaces  = text (/api/ask/stream) + voice (/api/voice/turn/stream)
  models    = auto / sonnet / opus / gemma(local) / claude_code
  personas  = every persona in settings.list_http_personas()

and that messaging a persona in /chat is IDENTICAL to messaging the matching
Telegram bot (same run_agent_loop, same persona preamble).

Modes:
  (default)  deterministic checks + two cheap liveness probes. No cloud cost
             beyond one Haiku voice turn; no Claude Code workers are spawned.
  --live     also runs the full live generation matrix (a real turn per model
             on text AND voice) and one live claude_code-via-voice handoff for a
             no-handoff persona — spawning a Claude Code worker that is then
             killed (process included, per issue #379).

Prints a human-readable report and a final `ROUTING-HEALTH-VERDICT:` line plus a
JSON blob the calling skill interprets. Read-mostly: the only writes are
throwaway probe conversations (deleted) and, under --live, one worker session
(killed). Run from the repo root.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[4]
os.chdir(REPO)
sys.path.insert(0, str(REPO))

BASE = os.environ.get("LIFEOS_BASE_URL", "http://localhost:8000")
WHISPER_RELAY = Path(os.environ.get("WHISPER_RELAY_DIR", str(Path.home() / "Code" / "whisper-relay")))
LIVE = "--live" in sys.argv or "--deep" in sys.argv
MODELS = ["auto", "opus", "sonnet", "gemma", "claude_code"]
BACKEND_HANDLED = {"auto", "sonnet", "opus", "gemma", "local", "claude_code"}

results: list[dict] = []          # {check, status(PASS/FAIL/WARN/SKIP), detail}
created_convs: list[str] = []     # conversation ids to clean up
PROBE = "routing-health probe — please ignore"


def record(check: str, status: str, detail: str = "") -> None:
    results.append({"check": check, "status": status, "detail": detail})
    print(f"  [{status:4}] {check}" + (f" — {detail}" if detail else ""))


def http_json(method: str, path: str, body: dict | None = None, timeout: int = 12):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
    try:
        return r.status, json.loads(raw)
    except Exception:
        return r.status, raw


# ---------------------------------------------------------------------------
# In-process harness: capture what chat.py dispatches to run_agent_loop.
# ---------------------------------------------------------------------------
_captured: list[dict] = []


async def _fake_loop(**kwargs):
    _captured.append(kwargs)
    yield {"type": "result", "result": SimpleNamespace(
        total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
        model="(mock)", tool_calls_log=[], full_text="(mock)")}


def _post(client, payload):
    """POST to the in-process app, swallowing the route's own print() noise."""
    with contextlib.redirect_stdout(io.StringIO()):
        return client.post("/api/ask/stream", json=payload)


def _conv_id(sse_text: str):
    for line in sse_text.splitlines():
        if line.startswith("data: "):
            try:
                d = json.loads(line[6:])
            except Exception:
                continue
            if d.get("type") == "conversation_id":
                return d.get("conversation_id")
    return None


def deterministic_checks():
    """Inventory, telegram equivalence, and the text routing matrix — all
    in-process with run_agent_loop and the worker spawn mocked (no LLM calls, no real workers)."""
    from unittest.mock import patch
    import api.services.agent_loop as agent_loop_mod
    from config.settings import settings
    from fastapi.testclient import TestClient
    from api.main import app

    client = TestClient(app)
    personas = settings.list_http_personas()
    persona_ids = [p.id for p in personas]
    print(f"\n## Inventory — {len(persona_ids)} personas: {persona_ids}")

    # --- A. Model-picker option values vs backend support -------------------
    html = (REPO / "web" / "index.html").read_text()
    m = re.search(r'id="modelPicker".*?</select>', html, re.S)
    picker_vals = re.findall(r'<option value="([^"]+)"', m.group(0)) if m else []
    print(f"## Model picker values: {picker_vals}")
    unknown = [v for v in picker_vals if v not in BACKEND_HANDLED
               and agent_loop_mod.resolve_model_alias(v) == v]
    if not picker_vals:
        record("inventory/model-picker-options", "FAIL", "could not parse #modelPicker options")
    elif unknown:
        record("inventory/model-picker-options", "FAIL",
               f"picker has values the backend does not handle: {unknown}")
    else:
        record("inventory/model-picker-options", "PASS",
               f"{len(picker_vals)} options, all backend-handled: {picker_vals}")

    # --- B. /api/personas matches the Telegram bot registry -----------------
    try:
        _, live_personas = http_json("GET", "/api/personas")
        live_ids = sorted(p["id"] for p in (
            live_personas if isinstance(live_personas, list)
            else live_personas.get("personas", live_personas.get("items", []))))
        if live_ids == sorted(persona_ids):
            record("equivalence/personas-vs-registry", "PASS",
                   f"/api/personas == settings registry ({live_ids})")
        else:
            record("equivalence/personas-vs-registry", "FAIL",
                   f"/api/personas={live_ids} != registry={sorted(persona_ids)}")
    except Exception as e:
        record("equivalence/personas-vs-registry", "WARN", f"live /api/personas unreachable: {e}")

    # --- C. Telegram-bot persona preamble == resolve_persona(id) ------------
    bots = {b.name: b.persona for b in settings.telegram_bots}
    mism = []
    for pid in persona_ids:
        resolved = settings.resolve_persona(pid)
        expected = settings.telegram_primary_bot.persona if pid == "primary" else bots.get(pid)
        if resolved is None or (pid != "primary" and pid not in bots) or resolved != expected:
            mism.append(pid)
    if mism:
        record("equivalence/persona-preamble-source", "FAIL",
               f"resolve_persona != bot.persona for: {mism}")
    else:
        record("equivalence/persona-preamble-source", "PASS",
               "resolve_persona(id) == matching Telegram bot.persona for every persona")

    # Orchestrating personas (doctor) take the Phase 5 spawn path on the
    # persona_id surface — they never reach run_agent_loop — so the inline
    # equivalence/matrix checks below skip them; check F covers their spawn.
    orchestrating = {pid for pid in persona_ids if settings.persona_orchestrates(pid)}

    # --- D. Empirical: /chat persona_id path == Telegram persona path -------
    eq_fail = []
    for pid in persona_ids:
        if pid in orchestrating:
            continue
        pre = settings.resolve_persona(pid)
        _captured.clear()
        with patch.object(agent_loop_mod, "run_agent_loop", _fake_loop):
            r1 = _post(client, {"question": PROBE, "persona_id": pid})
        chat_persona = _captured[-1].get("persona") if _captured else "<none>"
        created_convs.append(_conv_id(r1.text))
        _captured.clear()
        with patch.object(agent_loop_mod, "run_agent_loop", _fake_loop):
            r2 = _post(client, {"question": PROBE, "persona": pre})
        tg_persona = _captured[-1].get("persona") if _captured else "<none>"
        created_convs.append(_conv_id(r2.text))
        if not (chat_persona == tg_persona == pre):
            eq_fail.append(f"{pid}(chat={chat_persona!r},tg={tg_persona!r})")
    if eq_fail:
        record("equivalence/chat-vs-telegram-orchestrator-input", "FAIL", "; ".join(eq_fail))
    else:
        record("equivalence/chat-vs-telegram-orchestrator-input", "PASS",
               "run_agent_loop receives identical persona via /chat persona_id and Telegram persona (inline personas)")

    # --- E. TEXT routing matrix: persona x model dispatch -------------------
    def expected_model(mo):
        if mo == "auto":
            return getattr(settings, "anthropic_model", "claude-haiku-4-5")
        if mo == "gemma":
            return "local"
        if mo in ("opus", "sonnet"):
            return agent_loop_mod.resolve_model_alias(mo)
        return None

    npass = ntotal = 0
    for pid in persona_ids:
        if pid in orchestrating:
            continue
        pre = settings.resolve_persona(pid)
        for mo in MODELS:
            ntotal += 1
            _captured.clear()
            payload = {"question": PROBE, "persona_id": pid}
            if mo != "auto":
                payload["model_override"] = mo
            with patch.object(agent_loop_mod, "run_agent_loop", _fake_loop):
                r = _post(client, payload)
            created_convs.append(_conv_id(r.text))
            ok = False
            if mo == "claude_code":
                t = r.text
                ok = ('"type": "claude_intent"' in t and '"engine": "claude_code"' in t
                      and '"type": "content"' not in t and len(_captured) == 0)
            elif _captured:
                k = _captured[-1]
                ok = (k.get("model_tier") == expected_model(mo)
                      and bool(k.get("force_local")) == (mo == "gemma")
                      and k.get("persona") == pre)
            npass += 1 if ok else 0
            if not ok:
                record(f"text-matrix/{pid}/{mo}", "FAIL", json.dumps(_captured[-1] if _captured else {}, default=str)[:160])
    if npass == ntotal:
        record("text-matrix/all-cells", "PASS", f"{npass}/{ntotal} (persona x model) inline cells dispatch correctly")
    else:
        record("text-matrix/all-cells", "FAIL", f"{npass}/{ntotal} cells passed — see per-cell failures above")

    # --- F. Orchestrating personas spawn a Claude Code session, not inline ---
    # Mock the spawn so this stays deterministic (no real worker is launched).
    from api.services.agent_worker import claude_code_spawn as _ccs
    from api.services.agent_worker import session_store as _ss
    if orchestrating:
        spawn_calls: list[dict] = []

        def _fake_spawn(store, prompt, **kw):
            spawn_calls.append({"prompt": prompt, **kw})
            return {"ok": True, "session_id": "sess_routingcheck"}

        f_fail = []
        for pid in sorted(orchestrating):
            _captured.clear()
            spawn_calls.clear()
            with patch.object(agent_loop_mod, "run_agent_loop", _fake_loop), \
                 patch.object(_ccs, "spawn_claude_code_session", _fake_spawn), \
                 patch.object(_ss, "SessionStore", lambda *a, **k: object()):
                r = _post(client, {"question": PROBE, "persona_id": pid})
            created_convs.append(_conv_id(r.text))
            spawned = len(spawn_calls) == 1 and spawn_calls[0].get("bot") == pid
            if not spawned or _captured:
                f_fail.append(f"{pid}(spawned={spawned},inlined={bool(_captured)})")
        if f_fail:
            record("orchestrating/spawn-not-inline", "FAIL", "; ".join(f_fail))
        else:
            record("orchestrating/spawn-not-inline", "PASS",
                   f"{sorted(orchestrating)} spawn a bot-tagged Claude Code session instead of the inline loop")

    return persona_ids


def voice_forwarding_checks():
    """whisper-relay forwards model_override + bypasses the persona handoff gate
    for claude_code. Prefer running its test suite; fall back to a static check."""
    if not WHISPER_RELAY.exists():
        record("voice/whisper-relay-present", "WARN",
               f"whisper-relay not found at {WHISPER_RELAY} (set WHISPER_RELAY_DIR); voice forwarding unverified")
        return
    adapter = WHISPER_RELAY / "src" / "voice_gateway" / "adapters" / "lifeos.py"
    txt = adapter.read_text() if adapter.exists() else ""
    if "model_override" in txt and "handoff_override_for_model" in txt:
        record("voice/whisper-relay-forwarding", "PASS",
               "adapter forwards model_override and has handoff_override_for_model (claude_code gate bypass)")
    else:
        record("voice/whisper-relay-forwarding", "FAIL",
               "adapter missing model_override forwarding or handoff_override_for_model")
    test_file = WHISPER_RELAY / "tests" / "test_model_override.py"
    if test_file.exists():
        py = os.environ.get("WHISPER_RELAY_PYTHON", sys.executable)
        proc = subprocess.run([py, "-m", "pytest", "tests/test_model_override.py", "-q"],
                              cwd=WHISPER_RELAY, capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": "src"})
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-1] if (proc.stdout or proc.stderr) else ""
        record("voice/whisper-relay-tests", "PASS" if proc.returncode == 0 else "FAIL", tail)
    else:
        record("voice/whisper-relay-tests", "WARN", "tests/test_model_override.py not found")


def liveness_probes():
    """Cheap live probes against the DEPLOYED service: the claude_code
    short-circuit (no spawn) and one Haiku voice turn (the real chain)."""
    # Live claude_intent (no worker spawned — the SSE event only).
    try:
        req = urllib.request.Request(
            BASE + "/api/ask/stream", method="POST",
            data=json.dumps({"question": PROBE, "model_override": "claude_code"}).encode(),
            headers={"Content-Type": "application/json"})
        body = urllib.request.urlopen(req, timeout=20).read().decode()
        created_convs.append(_conv_id(body))
        if '"type": "claude_intent"' in body and '"type": "content"' not in body:
            record("live/text-claude_code-shortcircuit", "PASS", "deployed /api/ask/stream emits claude_intent, no inline answer")
        else:
            record("live/text-claude_code-shortcircuit", "FAIL", "claude_intent not emitted by deployed service")
    except Exception as e:
        record("live/text-claude_code-shortcircuit", "FAIL", f"request failed: {e}")

    # Live voice chain (auto + primary): proxy -> gateway -> /api/ask/stream -> TTS.
    cell = _voice_turn("primary", "auto")
    if cell.get("response"):
        record("live/voice-chain-auto-primary", "PASS",
               f"voice turn answered ({cell['routing']}); resp={cell['response'][:40]!r}")
    else:
        record("live/voice-chain-auto-primary", "FAIL", f"no voice response (chain broken): {cell}")


def _voice_turn(persona: str, model: str, transcript: str = "what is two plus two") -> dict:
    fields = ["-F", "transcript=" + transcript, "-F", "backend=lifeos", "-F", "persona_id=" + persona]
    if model and model != "auto":
        fields += ["-F", "model_override=" + model]
    out = subprocess.run(["curl", "-s", "-m", "150", "-N", "-X", "POST", BASE + "/api/voice/turn/stream"] + fields,
                         capture_output=True, text=True).stdout
    conv = resp = handoff = None
    for line in out.splitlines():
        if line.startswith("data: "):
            try:
                d = json.loads(line[6:])
            except Exception:
                continue
            if d.get("type") == "done":
                dd = d.get("data", {})
                conv, resp, handoff = dd.get("conversation_id"), dd.get("response_text"), dd.get("handoff")
    if conv:
        created_convs.append(conv)
    routing = None
    if conv:
        try:
            _, c = http_json("GET", "/api/conversations/" + conv)
            for msg in reversed(c.get("messages", []) if isinstance(c, dict) else []):
                if msg.get("role") == "assistant":
                    routing = (msg.get("routing") or {}).get("reasoning")
                    break
        except Exception:
            pass
    return {"persona": persona, "model": model, "conv": conv, "response": resp, "handoff": handoff, "routing": routing}


def live_matrix(persona_ids: list[str]):
    """Expensive end-to-end: a real generation per model on text and voice, and
    one claude_code-via-voice handoff (spawned worker, then killed)."""
    from config.settings import settings
    import api.services.agent_loop as agent_loop_mod

    def exp(mo):
        return ({"auto": getattr(settings, "anthropic_model", "claude-haiku-4-5"),
                 "gemma": "local"}.get(mo) or agent_loop_mod.resolve_model_alias(mo))

    # Spread models across personas so each persona is exercised live too.
    plan = list(zip(["auto", "opus", "sonnet", "gemma"], (persona_ids * 4)))
    # --- live TEXT generation per model ---
    for mo, pid in plan:
        try:
            payload = {"question": "what is two plus two", "persona_id": pid}
            if mo != "auto":
                payload["model_override"] = mo
            req = urllib.request.Request(BASE + "/api/ask/stream", method="POST",
                                         data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            body = urllib.request.urlopen(req, timeout=120).read().decode()
            created_convs.append(_conv_id(body))
            want = exp(mo)
            ok = (f"({want})" in body) or (want in body)
            record(f"live-text/{mo}+{pid}", "PASS" if ok else "FAIL",
                   f"expected routing model {want}" + ("" if ok else " not found in stream"))
        except Exception as e:
            record(f"live-text/{mo}+{pid}", "FAIL", f"request failed: {e}")

    # --- live VOICE generation per model ---
    for mo, pid in plan:
        cell = _voice_turn(pid, mo)
        want = exp(mo)
        ok = cell.get("routing") and (want in cell["routing"])
        record(f"live-voice/{mo}+{pid}", "PASS" if ok else "FAIL",
               f"routing={cell.get('routing')!r} (want {want})")

    # --- live claude_code via VOICE for a NO-HANDOFF persona (hardest cell) ---
    no_handoff = next((p.id for p in settings.list_http_personas() if not p.capabilities), persona_ids[-1])
    cell = _voice_turn(no_handoff, "claude_code", transcript="please refactor the nonexistent zztest sample module")
    ho = cell.get("handoff") or {}
    sid = ho.get("session_id") if isinstance(ho, dict) else None
    if cell.get("handoff") and sid:
        record(f"live-voice/claude_code+{no_handoff}", "PASS",
               f"handoff fired for no-handoff persona; spawned {sid}")
        # Kill the session AND its OS subprocess (HTTP kill alone leaves it — issue #379).
        try:
            http_json("POST", f"/api/agents/sessions/{sid}/kill", {"reason": "routing-health cleanup"}, timeout=20)
        except Exception as e:
            record("live-voice/claude_code-kill", "WARN", f"kill endpoint error: {e}")
        subprocess.run(["pkill", "-9", "-f", sid], capture_output=True)
        record("live-voice/claude_code-cleanup", "PASS", f"killed session + subprocess for {sid}")
    else:
        record(f"live-voice/claude_code+{no_handoff}", "FAIL", f"handoff did not fire: {cell}")


def cleanup():
    n = 0
    for cid in [c for c in dict.fromkeys(created_convs) if c]:
        try:
            urllib.request.urlopen(urllib.request.Request(
                BASE + "/api/conversations/" + cid, method="DELETE"), timeout=8)
            n += 1
        except Exception:
            pass
    print(f"\n## Cleanup: deleted {n} probe conversations")


def main():
    print("# ROUTING-HEALTH" + ("  (--live)" if LIVE else ""))
    print("\n## Deterministic checks (mocked dispatch — no LLM calls, no spawns)")
    try:
        persona_ids = deterministic_checks()
    except Exception as e:
        record("deterministic-harness", "FAIL", f"in-process harness crashed: {e}")
        persona_ids = []
    print("\n## Voice forwarding + gate")
    voice_forwarding_checks()
    print("\n## Liveness (deployed service)")
    liveness_probes()
    if LIVE and persona_ids:
        print("\n## Live generation matrix (real turns; spawns + kills one worker)")
        live_matrix(persona_ids)
    cleanup()

    fails = [r for r in results if r["status"] == "FAIL"]
    warns = [r for r in results if r["status"] == "WARN"]
    verdict = "FAILING" if fails else ("DEGRADED" if warns else "HEALTHY")
    summary = {"verdict": verdict, "live": LIVE,
               "counts": {s: sum(1 for r in results if r["status"] == s) for s in ("PASS", "FAIL", "WARN", "SKIP")},
               "failures": [{"check": r["check"], "detail": r["detail"]} for r in fails],
               "warnings": [{"check": r["check"], "detail": r["detail"]} for r in warns]}
    print(f"\nROUTING-HEALTH-VERDICT: {verdict}")
    print("ROUTING-HEALTH-JSON: " + json.dumps(summary))
    sys.exit(1 if verdict == "FAILING" else 0)


if __name__ == "__main__":
    main()
