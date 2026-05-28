"""Per-session "what did this agent work on?" summaries for the /agents UI.

Extracts a small context window from a session's transcript — the first user
message, the final assistant message, and any PR titles/URLs mentioned — and
asks the local Gemma LLM to produce two outputs:

    short_label: 2-6 word node label
    summary:     1-2 sentence / bullet recap

Results are cached by (session_id, last_activity_at) so a session that hasn't
moved doesn't re-summarize on every panel open or snapshot tick.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from api.services.llm_client import extract_json, generate_text

logger = logging.getLogger(__name__)

# Cap how much text we ship to Gemma. Generous enough that a chunky final
# message survives, tight enough that a 5000-event transcript doesn't drown
# the prompt.
_FIRST_MSG_CAP = 1500
_FINAL_MSG_CAP = 2500
_PR_CAP = 8

_GH_PR_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+")
_GH_PR_CREATE_RE = re.compile(r"gh\s+pr\s+create\b", re.IGNORECASE)


@dataclass(frozen=True)
class SummaryResult:
    short_label: str
    summary: str

    def as_dict(self) -> dict[str, str]:
        return {"short_label": self.short_label, "summary": self.summary}


# Cache key: session_id → (last_activity_at, SummaryResult). When the session's
# last_activity_at advances, the entry is recomputed.
_cache: dict[str, tuple[float, SummaryResult]] = {}
_CACHE_MAX = 500


def _trim(text: str, cap: int) -> str:
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    return text[: cap - 1] + "…"


def _extract_text_from_payload(payload: Any) -> str:
    """Pull a plain-text body from a normalized transcript payload."""
    if not isinstance(payload, dict):
        return ""
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text
    # CC assistant_message has tool_uses but no text — return ""
    final_text = payload.get("final_text")
    if isinstance(final_text, str):
        return final_text
    return ""


def _scan_prs(events: list[dict[str, Any]]) -> list[str]:
    """Find PR references in tool calls and assistant text.

    Returns a list of short descriptors (URL or `gh pr create` title fragment).
    Deduped, capped at _PR_CAP.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        s = s.strip()
        if not s or s in seen:
            return
        seen.add(s)
        found.append(s)

    for ev in events:
        if len(found) >= _PR_CAP:
            break
        payload = ev.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        kind = ev.get("kind") or ""

        # LifeOS-style tool_call payload
        if kind == "tool_call":
            tool = str(payload.get("tool") or "")
            args = payload.get("arguments") or {}
            if isinstance(args, dict):
                cmd = str(args.get("command") or "")
                if tool == "Bash" and _GH_PR_CREATE_RE.search(cmd):
                    title_match = re.search(r"--title\s+['\"]([^'\"]+)", cmd)
                    body_match = re.search(r"--body\s+['\"]([^'\"]+)", cmd)
                    bits = []
                    if title_match:
                        bits.append(title_match.group(1))
                    if body_match:
                        bits.append(body_match.group(1)[:200])
                    if bits:
                        add(" — ".join(bits))
                    else:
                        add("gh pr create (no title parsed)")

        # Free-form text in any payload — assistant_message / completed / etc.
        for key in ("text", "final_text"):
            val = payload.get(key)
            if isinstance(val, str):
                for m in _GH_PR_URL_RE.findall(val):
                    add(m)

        # CC tool_result content_preview can contain a PR URL too
        results = payload.get("tool_results")
        if isinstance(results, list):
            for tr in results:
                if isinstance(tr, dict):
                    preview = tr.get("content_preview")
                    if isinstance(preview, str):
                        for m in _GH_PR_URL_RE.findall(preview):
                            add(m)
    return found


def _extract_context(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Pull the three context fragments Gemma will summarize."""
    first_user = ""
    final_assistant = ""

    for ev in events:
        if first_user:
            break
        kind = ev.get("kind") or ""
        if kind in ("user_message", "seed"):
            txt = _extract_text_from_payload(ev.get("payload"))
            if txt:
                first_user = txt

    # Walk backwards for the final assistant/completed message.
    for ev in reversed(events):
        if final_assistant:
            break
        kind = ev.get("kind") or ""
        if kind in ("assistant_message", "completed"):
            txt = _extract_text_from_payload(ev.get("payload"))
            if txt:
                final_assistant = txt

    return {
        "first_user": _trim(first_user, _FIRST_MSG_CAP),
        "final_assistant": _trim(final_assistant, _FINAL_MSG_CAP),
        "prs": _scan_prs(events),
    }


def _build_prompt(label: str, ctx: dict[str, Any]) -> str:
    parts: list[str] = [
        "You are summarizing an AI coding agent's work session for a status dashboard.",
        "Return STRICT JSON with two fields:",
        '  "short_label": 2-6 words, Title Case, no trailing period — a node label',
        '  "summary": 1-2 sentences OR up to 4 short bullets (markdown "- "), max ~80 words',
        "",
        "Focus on WHAT the agent worked on / accomplished, not the framing.",
        "Be concrete: name the feature/bug/file area. Do not hedge.",
        "",
        f"Session label (from task description): {label}",
        "",
    ]
    if ctx["first_user"]:
        parts.append("--- Original request ---")
        parts.append(ctx["first_user"])
        parts.append("")
    if ctx["prs"]:
        parts.append("--- Pull requests produced ---")
        for pr in ctx["prs"]:
            parts.append(f"- {pr}")
        parts.append("")
    if ctx["final_assistant"]:
        parts.append("--- Final agent message ---")
        parts.append(ctx["final_assistant"])
        parts.append("")
    parts.append('Respond with JSON only, no prose: {"short_label": "...", "summary": "..."}')
    return "\n".join(parts)


def _fallback_label(label: str) -> str:
    """Heuristic short label when LLM is unavailable or returns garbage."""
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]*", label or "")
    return " ".join(words[:5]).strip() or "Untitled"


async def summarize_session(
    session_id: str,
    *,
    label: str,
    last_activity_at: float,
    events: list[dict[str, Any]],
) -> SummaryResult:
    """Return cached summary or compute a fresh one.

    Caller passes events (already fetched) so this module doesn't take a
    dependency on the transcript store / claude_code ingest dispatch logic
    that already lives in api/routes/agents.py.
    """
    cached = _cache.get(session_id)
    if cached and cached[0] >= last_activity_at:
        return cached[1]

    ctx = _extract_context(events)
    # If we have nothing to summarize, return a deterministic fallback so the
    # UI still gets *something*. Don't cache (next time we may have content).
    if not ctx["first_user"] and not ctx["final_assistant"] and not ctx["prs"]:
        return SummaryResult(
            short_label=_fallback_label(label),
            summary="(No transcript content yet.)",
        )

    prompt = _build_prompt(label, ctx)
    try:
        # Gemma 4 burns most of its budget on reasoning_content; only
        # `content` is returned in `text` by LocalLLMClient. Anything under
        # ~3000 tokens regularly hits the cap mid-reasoning, leaving content
        # empty. Observed real CC sessions used ~2400 completion tokens
        # (≈9.5k chars reasoning + 200 chars JSON), so 4096 is the floor.
        #
        # 240s timeout: a fresh fast-path summary lands in ~25-30s, but when
        # Gemma is already serving chat / the agent worker, the queue can push
        # past 120s. The frontend has its own 5-minute abort, so this is the
        # outer bound for "Gemma will finish eventually" before we give up.
        text = await generate_text(
            prompt,
            max_tokens=4096,
            temperature=0.2,
            timeout=240.0,
        )
        data = extract_json(text)
        short_label = str(data.get("short_label") or "").strip()
        summary = str(data.get("summary") or "").strip()
        if not short_label:
            short_label = _fallback_label(label)
        if not summary:
            summary = "(empty summary)"
        # Clamp short_label hard so a runaway response doesn't blow up the node.
        words = short_label.split()
        if len(words) > 7:
            short_label = " ".join(words[:7])
    except Exception as exc:  # noqa: BLE001 — never break the panel on summary failure
        logger.warning("session summary failed for %s: %s", session_id, exc)
        return SummaryResult(
            short_label=_fallback_label(label),
            summary=f"(Summary unavailable: {type(exc).__name__})",
        )

    result = SummaryResult(short_label=short_label, summary=summary)
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[session_id] = (last_activity_at, result)
    return result


def get_cached_summary(session_id: str, last_activity_at: float) -> SummaryResult | None:
    """Synchronous peek for the snapshot path — never triggers an LLM call."""
    cached = _cache.get(session_id)
    if cached and cached[0] >= last_activity_at:
        return cached[1]
    return None


def reset_cache() -> None:
    _cache.clear()
