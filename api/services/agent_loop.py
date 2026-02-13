"""
Agentic chat loop for LifeOS.

Runs a multi-turn conversation with Claude where the model can call tools
autonomously. Implemented as an async generator that yields events so the
caller (SSE endpoint) can stream them to the client in real time.

Event types yielded:
  {"type": "text",   "content": "..."}       -- streamed text chunk
  {"type": "status", "message": "..."}       -- tool execution status
  {"type": "result", "result": AgentResult}  -- final result (last event)
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import AsyncGenerator

from config.settings import settings
from api.services.model_selector import get_claude_model_name
from api.services.agent_system_prompt import build_system_prompt
from api.services.agent_tools import TOOL_DEFINITIONS, TOOL_STATUS_MESSAGES, execute_tool_parallel
from api.services.synthesizer import build_message_content

logger = logging.getLogger(__name__)

# Pricing per million tokens
_PRICING = {
    "haiku":  {"input": 0.80,  "output": 4.00},
    "sonnet": {"input": 3.00,  "output": 15.00},
    "opus":   {"input": 15.00, "output": 75.00},
}

# Consolidated tools that use sub-action status messages
_CONSOLIDATED_TOOLS = {"manage_tasks", "manage_reminders", "person_info"}


def _calc_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> float:
    """Calculate cost in USD accounting for prompt caching.

    Anthropic API returns input_tokens as non-cached count (excludes cache tokens).
    Cache reads cost 0.1x and cache creation costs 1.25x the input price.
    """
    tier = "sonnet"
    model_lower = model.lower()
    if "haiku" in model_lower:
        tier = "haiku"
    elif "opus" in model_lower:
        tier = "opus"
    pricing = _PRICING[tier]
    return (
        (input_tokens / 1_000_000) * pricing["input"]
        + (cache_read / 1_000_000) * pricing["input"] * 0.1
        + (cache_creation / 1_000_000) * pricing["input"] * 1.25
        + (output_tokens / 1_000_000) * pricing["output"]
    )


@dataclass
class AgentResult:
    """Result of an agentic chat loop run."""
    full_text: str
    tool_calls_log: list[dict] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    model: str = ""


async def run_agent_loop(
    question: str,
    conversation_history: list | None = None,
    attachments: list[dict] | None = None,
    model_tier: str = "sonnet",
    max_tool_rounds: int = 5,
) -> AsyncGenerator[dict, None]:
    """
    Async generator that runs the agentic chat loop.

    Yields events as they happen so the caller can stream them.

    Args:
        question: The user's current question.
        conversation_history: Previous messages (list of Message objects with .role, .content).
        attachments: Optional file attachments (list of dicts with filename, media_type, data).
        model_tier: "haiku", "sonnet", or "opus".
        max_tool_rounds: Max number of tool-use rounds before forcing a text response.

    Yields:
        Dicts with "type" key: "text", "status", or "result".
    """
    import anthropic

    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=90.0,  # 90s per API call (default is 600s)
    )
    model = get_claude_model_name(model_tier)
    system_prompt = build_system_prompt()

    # Build messages array from conversation history
    messages = []
    if conversation_history:
        for msg in conversation_history[-10:]:
            if msg.role in ("user", "assistant") and msg.content:
                messages.append({"role": msg.role, "content": msg.content})

    # Add current user message (with attachments if any)
    user_content = build_message_content(question, attachments)
    messages.append({"role": "user", "content": user_content})

    result = AgentResult(full_text="", model=model)

    async def _stream_round(call_kwargs: dict):
        """Run one Claude API round. Returns (text, final_msg)."""
        text = ""
        async with client.messages.stream(**call_kwargs) as stream:
            async for event in stream:
                if hasattr(event, "type") and event.type == "content_block_delta":
                    delta = event.delta
                    if hasattr(delta, "text") and delta.text:
                        text += delta.text
                        yield delta.text
            yield await stream.get_final_message()

    def _track_usage(final_msg):
        if final_msg and final_msg.usage:
            usage = final_msg.usage
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
            result.total_input_tokens += usage.input_tokens
            result.total_output_tokens += usage.output_tokens
            result.total_cache_read_tokens += cache_read
            result.total_cache_creation_tokens += cache_creation
            result.total_cost_usd += _calc_cost(
                model, usage.input_tokens, usage.output_tokens,
                cache_read=cache_read, cache_creation=cache_creation,
            )

    for round_num in range(1, max_tool_rounds + 1):
        print(f"[agent] Round {round_num}/{max_tool_rounds} starting")
        call_kwargs = {
            "model": model,
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": messages,
            "tools": TOOL_DEFINITIONS,
        }

        text_this_round = ""
        tool_use_blocks = []
        final_msg = None

        try:
            async for item in _stream_round(call_kwargs):
                if isinstance(item, str):
                    text_this_round += item
                    yield {"type": "text", "content": item}
                else:
                    final_msg = item
        except Exception as e:
            print(f"[agent] Round {round_num} API error: {e}")
            if result.full_text:
                yield {"type": "text", "content": f"\n\n(Search interrupted: {e})"}
            else:
                yield {"type": "text", "content": f"Sorry, I encountered an error: {e}"}
            break

        _track_usage(final_msg)
        result.full_text += text_this_round

        # Extract tool use blocks from the final message
        assistant_content = []
        for block in final_msg.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                tool_use_blocks.append(block)
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        tool_names = [b.name for b in tool_use_blocks]
        print(f"[agent] Round {round_num} done: stop={final_msg.stop_reason}, tools={tool_names}, text={len(text_this_round)}ch")

        # If no tool calls, we're done
        if final_msg.stop_reason != "tool_use" or not tool_use_blocks:
            break

        # Append the assistant message with tool use blocks
        messages.append({"role": "assistant", "content": assistant_content})

        # Execute tools in parallel
        async def _exec_one(block):
            name = block.name
            logger.info(f"Executing tool: {name} with input: {block.input}")
            tool_result_str = await execute_tool_parallel(name, block.input)
            is_error = tool_result_str.startswith("Error:")
            result.tool_calls_log.append({
                "tool": name,
                "input": block.input,
                "result_preview": tool_result_str[:200],
                "is_error": is_error,
            })
            return {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": tool_result_str,
                "is_error": is_error,
            }

        # Emit status for each tool (with sub-action lookup for consolidated tools)
        for block in tool_use_blocks:
            status_msg = TOOL_STATUS_MESSAGES.get(block.name, f"Running {block.name}...")
            if block.name in _CONSOLIDATED_TOOLS:
                action = block.input.get("action", "")
                sub_key = f"{block.name}.{action}"
                status_msg = TOOL_STATUS_MESSAGES.get(sub_key, status_msg)
            yield {"type": "status", "message": status_msg}

        tool_results = await asyncio.gather(*[_exec_one(b) for b in tool_use_blocks])
        print(f"[agent] Round {round_num} tools executed: {[b.name for b in tool_use_blocks]}")

        # Append tool results as a user message
        messages.append({"role": "user", "content": list(tool_results)})

    else:
        # Exhausted all tool rounds — force a final synthesis round without tools
        print("[agent] Exhausted tool rounds, running synthesis round")
        call_kwargs = {
            "model": model,
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": messages,
        }
        final_msg = None
        try:
            async for item in _stream_round(call_kwargs):
                if isinstance(item, str):
                    result.full_text += item
                    yield {"type": "text", "content": item}
                else:
                    final_msg = item
        except Exception as e:
            print(f"[agent] Synthesis round error: {e}")
            yield {"type": "text", "content": f"\n\n(Error during synthesis: {e})"}
        _track_usage(final_msg)

    print(f"[agent] Loop complete: {len(result.tool_calls_log)} tool calls, {len(result.full_text)}ch text")
    # Yield the final result
    yield {"type": "result", "result": result}
