"""
Web search service for LifeOS.

Real web search on every LLM backend. The search itself runs in LifeOS via
DuckDuckGo (no API key), so it works identically whether the orchestrator is on
Anthropic, Codex, or the local llama-server. On the Anthropic backend we upgrade
to Claude's native server-side web search (iterative, cited) for higher quality.

Both entry points degrade honestly: a failed or empty search returns nothing to
say (empty results / empty answer), never a fabricated "from my training data"
response — that stub behavior is exactly what this module replaces.
"""
import asyncio
import logging

from config.settings import settings

logger = logging.getLogger(__name__)


def _ddg_search(query: str, max_results: int = 5) -> list[dict]:
    """Real web results via DuckDuckGo (no API key), synchronous.

    Best-effort: returns [] on any failure (rate-limit, network, parse) so the
    caller degrades to "nothing found" rather than inventing an answer.
    """
    try:
        from ddgs import DDGS

        with DDGS() as ddgs:
            hits = ddgs.text(query, max_results=max_results)
    except Exception as e:
        logger.warning(f"DuckDuckGo search failed: {e}")
        return []
    out: list[dict] = []
    for h in hits or []:
        out.append({
            "title": h.get("title") or "",
            "url": h.get("href") or h.get("url") or "",
            "snippet": h.get("body") or h.get("snippet") or "",
        })
    return out


async def search_web(query: str, max_results: int = 5) -> list[dict]:
    """Real web search results, backend-independent (via DuckDuckGo).

    Returns a list of results, each with: title, url, snippet. [] on failure.
    The blocking fetch runs in a worker thread so it never stalls the event loop.
    """
    return await asyncio.to_thread(_ddg_search, query, max_results)


def format_web_results_for_context(results: list[dict]) -> str:
    """Format web search results as context for synthesis."""
    if not results:
        return "No web search results found."

    formatted = "Web Search Results:\n\n"
    for i, result in enumerate(results, 1):
        title = result.get("title", "Untitled")
        url = result.get("url", "")
        snippet = result.get("snippet", "")

        formatted += f"{i}. **{title}**\n"
        if url:
            formatted += f"   Source: {url}\n"
        if snippet:
            formatted += f"   {snippet}\n"
        formatted += "\n"

    return formatted.strip()


def _use_native_anthropic() -> bool:
    """Native Claude web search is the upgrade path when the orchestrator backend
    is Anthropic; every other backend (Codex, local) uses DuckDuckGo."""
    return getattr(settings, "llm_backend", "anthropic").lower() == "anthropic"


async def _anthropic_native_search(query: str) -> str:
    """Claude's native server-side web search (iterative, cited).

    Uses the active Anthropic orchestrator client (a current, web-search-capable
    model) — not the dedicated ``get_anthropic_llm()`` specialist client, which is
    pinned to a now-retired model. We declare the basic ``web_search_20250305``
    variant, which every current Claude model supports. Returns the synthesized
    answer text, or "" if the search produced nothing usable.
    """
    from api.services.llm_client import get_local_llm

    client = get_local_llm()
    resp = await client.acreate(
        messages=[{"role": "user", "content": query}],
        system=(
            "You are a web research assistant. Search the web and answer the "
            "question directly and concisely, noting your sources. If the search "
            "returns nothing useful, say so plainly rather than guessing."
        ),
        max_tokens=1024,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
    )
    return (resp.text or "").strip()


async def search_web_with_synthesis(query: str) -> tuple[str, list[dict]]:
    """Answer a web-style query with real, current results on every backend.

    - **Anthropic backend:** Claude's native web search (best quality) — returns
      its synthesized, cited answer.
    - **Codex / local (and as a fallback if the native path fails):** DuckDuckGo
      results, formatted for the caller to synthesize on its own backend.

    Returns ``(answer_or_context, raw_results)``. On a genuinely empty search,
    returns ``("", [])`` so the caller can say it found nothing — never a
    fabricated answer.
    """
    if _use_native_anthropic():
        try:
            answer = await _anthropic_native_search(query)
            if answer:
                return answer, [{"title": "Claude web search", "url": "", "snippet": answer[:500]}]
        except Exception as e:
            logger.warning(f"Native Anthropic web search failed; falling back to DuckDuckGo: {e}")

    results = await search_web(query)
    if not results:
        return "", []
    return format_web_results_for_context(results), results
