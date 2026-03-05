"""
Web search service for LifeOS.

Uses the local LLM's training knowledge to answer web-style queries.
The local model doesn't have real-time web access, but can answer many
factual questions from its training data. For truly real-time queries
(weather, stock prices, live scores), it will indicate the limitation.
"""
import logging
from typing import Optional

from api.services.llm_client import get_local_llm

logger = logging.getLogger(__name__)


async def search_web(query: str, max_results: int = 5) -> list[dict]:
    """
    Answer a web-style query using the local LLM.

    Args:
        query: The search query
        max_results: Maximum number of results to return

    Returns:
        List of results, each with: title, url, snippet
    """
    try:
        client = get_local_llm()
        response = client.create(
            messages=[{
                "role": "user",
                "content": (
                    f"Answer this query using your knowledge: {query}\n\n"
                    "If this requires real-time data (live prices, current weather, "
                    "today's news), say so clearly. Otherwise, provide the best "
                    "factual answer you can."
                ),
            }],
            system="You are a helpful assistant answering factual questions. Be concise and accurate.",
            max_tokens=1024,
        )
        return [{
            "title": "Knowledge Base Answer",
            "url": "",
            "snippet": response.text[:500],
        }]
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
    Answer a query and return both synthesized answer and raw results.

    Args:
        query: The search query

    Returns:
        Tuple of (synthesized_answer, raw_results)
    """
    try:
        client = get_local_llm()
        response = client.create(
            messages=[{"role": "user", "content": query}],
            system=(
                "You are a helpful assistant. Answer the question directly and concisely. "
                "If the question requires truly real-time data (live weather, current stock "
                "prices, today's breaking news), note that you don't have real-time web access "
                "but provide the best answer from your training knowledge."
            ),
            max_tokens=1024,
        )
        results = [{
            "title": "Answer",
            "url": "",
            "snippet": response.text[:500],
        }]
        return response.text, results
    except Exception as e:
        logger.error(f"Web search with synthesis failed: {e}")
        return f"I couldn't process this query: {str(e)}", []
