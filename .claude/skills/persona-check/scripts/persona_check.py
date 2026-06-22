#!/usr/bin/env python3
"""Persona-health check for the LifeOS persona layer.

Confirms: every persona file loads; the /chat surface matches the Telegram bot
registry per persona (same ids, same preamble — so messaging a persona is
identical to messaging its bot); NO configured personal values (names, paths)
leak into the committed persona files; and any YAML frontmatter is valid
(forward-compatible — silent when there's no frontmatter yet).

Settings + one HTTP call only — no app/TestClient load, so it's fast and safe to
run often. Run from the repo root. Read-only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
os.chdir(REPO)
sys.path.insert(0, str(REPO))
BASE = os.environ.get("LIFEOS_BASE_URL", "http://localhost:8000")
PDIR = REPO / "config" / "personas"

from config.settings import settings  # noqa: E402

results: list[dict] = []


def record(check: str, status: str, detail: str = "") -> None:
    results.append({"check": check, "status": status, "detail": detail})
    print(f"  [{status:4}] {check}" + (f" — {detail}" if detail else ""))


personas = settings.list_http_personas()
ids = [p.id for p in personas]
bots = {b.name: b for b in settings.telegram_bots}
registry = {}
_tb = REPO / "config" / "telegram_bots.json"
if _tb.exists():
    try:
        registry = {e.get("name"): e.get("persona_file") for e in json.loads(_tb.read_text())}
    except Exception:
        registry = {}
committed_md = [f for f in PDIR.glob("*.md") if not f.name.endswith(".local.md") and f.name != "README.md"]
print(f"## Personas: {ids}    files: {[f.name for f in committed_md]}")

# --- 1. File health -------------------------------------------------------
for p in personas:
    if p.id == "primary":
        f = PDIR / "primary.md"
        if f.exists() and f.read_text().strip():
            record("file/primary", "PASS", "primary.md present and non-empty")
        else:
            record("file/primary", "INFO", "no primary.md — primary's personality lives in the static prompt (today's default)")
        continue
    pre = settings.resolve_persona(p.id)
    pf = registry.get(p.id)
    if not pre:
        record(f"file/{p.id}", "FAIL", "resolve_persona returned empty — persona_file missing or unreadable")
    elif pf and not (REPO / pf).exists():
        record(f"file/{p.id}", "FAIL", f"registry persona_file {pf} does not exist on disk")
    else:
        record(f"file/{p.id}", "PASS", f"{pf or '(by convention)'} loads ({len(pre)} chars)")

# --- 2. Equivalence: /chat surface == Telegram registry -------------------
try:
    raw = urllib.request.urlopen(BASE + "/api/personas", timeout=10).read().decode()
    payload = json.loads(raw)
    live = sorted(x["id"] for x in (payload if isinstance(payload, list) else payload.get("personas", payload.get("items", []))))
    if live == sorted(ids):
        record("equivalence/personas-vs-registry", "PASS", "/api/personas == settings registry")
    else:
        record("equivalence/personas-vs-registry", "FAIL", f"/api/personas={live} != registry={sorted(ids)}")
except Exception as e:
    record("equivalence/personas-vs-registry", "WARN", f"live /api/personas unreachable ({e}); is lifeos-api up?")

mismatch = [p.id for p in personas
            if p.id != "primary" and (p.id not in bots or settings.resolve_persona(p.id) != bots[p.id].persona)]
record("equivalence/preamble-source", "FAIL" if mismatch else "PASS",
       f"resolve_persona != bot.persona for {mismatch}" if mismatch
       else "resolve_persona(id) == the matching Telegram bot.persona — /chat behaves identically to the bot")

cap_mismatch = [p.id for p in personas
                if p.id in bots and bool(p.capabilities) != bool(bots[p.id].orchestrates)]
record("equivalence/capabilities", "FAIL" if cap_mismatch else "PASS",
       f"capabilities/orchestrates mismatch: {cap_mismatch}" if cap_mismatch
       else "advertised capabilities match each bot's orchestrates flag")

# --- 3. Privacy guard: no configured personal values in committed files ----
denylist: dict[str, str] = {}
GENERIC = {"partner", "relationship", "user", "the", "and", "personal"}
for attr in ("user_name", "partner_name", "therapist_patterns", "personal_relationship_patterns"):
    val = (getattr(settings, attr, "") or "").strip()
    for tok in re.split(r"[,|/]", val):
        tok = tok.strip()
        if len(tok) >= 3 and tok.lower() not in GENERIC:
            denylist.setdefault(tok, attr)
_pd = REPO / "config" / "people_dictionary.json"
if _pd.exists():
    try:
        for name in json.loads(_pd.read_text()).keys():
            if len(name) >= 3 and name.lower() not in GENERIC:
                denylist.setdefault(name, "people_dictionary")
    except Exception:
        pass

leaks: list[str] = []
for f in committed_md:
    text = f.read_text()
    low = text.lower()
    for tok, src in denylist.items():
        if re.search(r"\b" + re.escape(tok.lower()) + r"\b", low):
            leaks.append(f"{f.name}: '{tok}' (configured as {src})")
    for pat in (r"/home/[a-z]", r"/Users/[a-z]", r"~/Notes"):
        if re.search(pat, text):
            leaks.append(f"{f.name}: personal path matching `{pat}`")
if not denylist:
    record("privacy/no-personal-values", "WARN", "no personal values configured to check against (settings empty?)")
elif leaks:
    record("privacy/no-personal-values", "FAIL", "; ".join(leaks[:8]))
else:
    record("privacy/no-personal-values", "PASS",
           f"{len(committed_md)} committed files clean of {len(denylist)} configured personal tokens + home paths")

# --- 4. Frontmatter validation (forward-compatible) -----------------------
try:
    import frontmatter as _fm
except Exception:
    _fm = None
fm_issues: list[str] = []
fm_count = 0
for f in committed_md:
    raw = f.read_text()
    if not raw.lstrip().startswith("---"):
        continue  # no frontmatter yet — today's state, fine
    fm_count += 1
    if _fm is None:
        fm_issues.append(f"{f.name}: python-frontmatter not importable")
        continue
    meta = _fm.loads(raw).metadata
    if meta.get("id") != f.stem:
        fm_issues.append(f"{f.name}: id={meta.get('id')!r} != filename {f.stem!r}")
    if "voice" in meta and not isinstance(meta["voice"], list):
        fm_issues.append(f"{f.name}: `voice` must be a list")
    if "model" in meta and not isinstance(meta["model"], str):
        fm_issues.append(f"{f.name}: `model` must be a string")
if fm_count == 0:
    record("schema/frontmatter", "INFO", "no persona frontmatter yet (pre-schema) — skipped")
elif fm_issues:
    record("schema/frontmatter", "FAIL", "; ".join(fm_issues))
else:
    record("schema/frontmatter", "PASS", f"{fm_count} files have valid frontmatter")

# --- verdict --------------------------------------------------------------
fails = [r for r in results if r["status"] == "FAIL"]
warns = [r for r in results if r["status"] == "WARN"]
verdict = "FAILING" if fails else ("DEGRADED" if warns else "HEALTHY")
print(f"\nPERSONA-CHECK-VERDICT: {verdict}")
print("PERSONA-CHECK-JSON: " + json.dumps({
    "verdict": verdict,
    "counts": {s: sum(1 for r in results if r["status"] == s) for s in ("PASS", "FAIL", "WARN", "INFO")},
    "failures": [{"check": r["check"], "detail": r["detail"]} for r in fails],
    "warnings": [{"check": r["check"], "detail": r["detail"]} for r in warns],
}))
sys.exit(1 if verdict == "FAILING" else 0)
