"""
Web search service using Claude's native web_search tool.

Provides web search capability for queries requiring external information
like weather, current events, prices, schedules, etc.
"""
import logging
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)


async def search_web(query: str, max_results: int = 5) -> list[dict]:
    """
    Search the web using Claude's native web_search tool.

    Args:
        query: The search query
        max_results: Maximum number of results to return

    Returns:
        List of results, each with: title, url, snippet
    """
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        # Use Claude with web_search tool to get search results
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            tools=[{"type": "web_search", "name": "web_search"}],
            messages=[
                {
                    "role": "user",
                    "content": f"Search the web for: {query}\n\nReturn the top {max_results} most relevant results."
                }
            ]
        )

        # Extract search results from the response
        results = []
        for block in response.content:
            if block.type == "tool_use" and block.name == "web_search":
                # The tool_use block contains the search query
                continue
            elif block.type == "web_search_tool_result":
                # Extract results from the web search tool result
                for search_result in getattr(block, 'search_results', []):
                    results.append({
                        "title": getattr(search_result, 'title', ''),
                        "url": getattr(search_result, 'url', ''),
                        "snippet": getattr(search_result, 'snippet', ''),
                    })

        # If we got results from the tool, return them
        if results:
            return results[:max_results]

        # Fallback: extract any text response that might contain synthesized info
        for block in response.content:
            if block.type == "text":
                # Claude synthesized an answer - return it as a single "result"
                return [{
                    "title": "Web Search Results",
                    "url": "",
                    "snippet": block.text[:500],
                }]

        return []

    except Exception as e:
        logger.error(f"Web search failed: {e}")
        return []


def format_web_results_for_context(results: list[dict]) -> str:
    """
    Format web search results as context for synthesis.

    Args:
        results: List of search results from search_web()

    Returns:
        Formatted string suitable for inclusion in synthesis prompt
    """
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


async def search_web_with_synthesis(query: str) -> tuple[str, list[dict]]:
    """
    Search the web and return both synthesized answer and raw results.

    This uses Claude with web search to get a direct answer plus the
    underlying search results for transparency.

    Args:
        query: The search query

    Returns:
        Tuple of (synthesized_answer, raw_results)
    """
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            tools=[{"type": "web_search", "name": "web_search"}],
            messages=[
                {
                    "role": "user",
                    "content": query
                }
            ]
        )

        # Extract both the synthesized text and raw results
        synthesized = ""
        results = []

        for block in response.content:
            if block.type == "text":
                synthesized = block.text
            elif block.type == "web_search_tool_result":
                for search_result in getattr(block, 'search_results', []):
                    results.append({
                        "title": getattr(search_result, 'title', ''),
                        "url": getattr(search_result, 'url', ''),
                        "snippet": getattr(search_result, 'snippet', ''),
                    })

        return synthesized, results

    except Exception as e:
        logger.error(f"Web search with synthesis failed: {e}")
        return f"I couldn't search the web: {str(e)}", []
