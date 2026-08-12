"""
The capabilities preamble injected at the top of every task message.

Why: the agent's preset system prompt is fixed in the Anthropic console and
covers persona/policy. It does NOT enumerate what data and tools LifeOS
makes available. Without that, the agent fumbles — searches with the wrong
terms, misses better tools, returns empty when its first try fails. This
preamble closes that gap on every task in ~500 tokens.

The text is intentionally compact. Anything longer is read on demand from
the vault itself (the agent has `lifeos_search` / `lifeos_ask`).

Source of truth is `config/agent_capabilities.md` — gitignored, because it
names the operator, their employer, and their vault layout. A fresh clone
falls back to `config/agent_capabilities.example.md`, which carries the same
structure with placeholders. Copy the example to `agent_capabilities.md` and
fill it in.
"""

from pathlib import Path

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
_LOCAL = _CONFIG_DIR / "agent_capabilities.md"
_EXAMPLE = _CONFIG_DIR / "agent_capabilities.example.md"


def _load_preamble() -> str:
    """Operator's own briefing if present, else the placeholder template."""
    for candidate in (_LOCAL, _EXAMPLE):
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    # Both files missing (unexpected): a minimal briefing still beats nothing.
    return "=== LIFEOS BRIEFING (read first) ===\n\n=== TASK ==="


CAPABILITIES_PREAMBLE = _load_preamble()
