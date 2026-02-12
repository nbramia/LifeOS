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
from api.services.agent_tools import TOOL_DEFINITIONS, TOOL_STATUS_MESSAGES, execute_tool
from api.services.synthesizer import build_message_content

logger = logging.getLogger(__name__)

# Pricing per million tokens
_PRICING = {
    "haiku":  {"input": 0.80,  "output": 4.00},
    "sonnet": {"input": 3.00,  "output": 15.00},
    "opus":   {"input": 15.00, "output": 75.00},
}


def _calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate cost in USD for a given model and token counts."""
    tier = "sonnet"
    model_lower = model.lower()
    if "haiku" in model_lower:
        tier = "haiku"
    elif "opus" in model_lower:
        tier = "opus"
    pricing = _PRICING[tier]
    return (input_tokens / 1_000_000) * pricing["input"] + (output_tokens / 1_000_000) * pricing["output"]


@dataclass
class AgentResult:
    """Result of an agentic chat loop run."""
    full_text: str
    tool_calls_log: list[dict] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
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

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
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

    for round_num in range(1, max_tool_rounds + 1):
        is_last_round = round_num >= max_tool_rounds

        # On last round, omit tools to force a text response
        call_kwargs = {
            "model": model,
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": messages,
        }
        if not is_last_round:
            call_kwargs["tools"] = TOOL_DEFINITIONS

        # Stream the response — sync streaming inside async generator is fine:
        # each yield suspends the generator, giving control back to the event loop
        text_this_round = ""
        tool_use_blocks = []

        with client.messages.stream(**call_kwargs) as stream:
            for event in stream:
                if hasattr(event, "type") and event.type == "content_block_delta":
                    delta = event.delta
                    if hasattr(delta, "text") and delta.text:
                        text_this_round += delta.text
                        yield {"type": "text", "content": delta.text}

            final_msg = stream.get_final_message()

        # Track usage
        if final_msg and final_msg.usage:
            usage = final_msg.usage
            result.total_input_tokens += usage.input_tokens
            result.total_output_tokens += usage.output_tokens
            result.total_cost_usd += _calc_cost(model, usage.input_tokens, usage.output_tokens)

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

        # If no tool calls, we're done
        if final_msg.stop_reason != "tool_use" or not tool_use_blocks:
            break

        # Append the assistant message with tool use blocks
        messages.append({"role": "assistant", "content": assistant_content})

        # Execute tools in parallel
        async def _exec_one(block):
            name = block.name
            logger.info(f"Executing tool: {name} with input: {block.input}")
            tool_result_str = await execute_tool(name, block.input)
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

        # Emit status for each tool
        for block in tool_use_blocks:
            status_msg = TOOL_STATUS_MESSAGES.get(block.name, f"Running {block.name}...")
            yield {"type": "status", "message": status_msg}

        tool_results = await asyncio.gather(*[_exec_one(b) for b in tool_use_blocks])

        # Append tool results as a user message
        messages.append({"role": "user", "content": list(tool_results)})

    # Yield the final result
    yield {"type": "result", "result": result}
