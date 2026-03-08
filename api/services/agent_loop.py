"""
Agentic chat loop for LifeOS.

Runs a multi-turn conversation where the model can call tools
autonomously. Implemented as an async generator that yields events so the
caller (SSE endpoint) can stream them to the client in real time.

Uses the local LLM (OpenAI-compatible llama-server) by default.

Event types yielded:
  {"type": "text",   "content": "..."}       -- streamed text chunk
  {"type": "status", "message": "..."}       -- tool execution status
  {"type": "self_correction"}                -- model retrying (consumers should clear buffered text)
  {"type": "result", "result": AgentResult}  -- final result (last event)
"""
import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import AsyncGenerator
from api.services.agent_system_prompt import build_system_prompt
from api.services.agent_tools import TOOL_DEFINITIONS, TOOL_STATUS_MESSAGES, execute_tool_parallel
from api.services.synthesizer import build_message_content
from api.services.perf_trace import trace_span
from api.services.llm_client import get_local_llm, openai_tool_calls_to_anthropic, LLMUsage
from api.services.resilience import is_retryable_api_error

logger = logging.getLogger(__name__)

# Consolidated tools that use sub-action status messages
_CONSOLIDATED_TOOLS = {"manage_tasks", "manage_reminders", "person_info"}

# Patterns that indicate the model is giving up without trying tools
_GIVE_UP_PATTERNS = re.compile(
    r"(?i)("
    r"can'?t access|cannot access|unable to access"
    r"|can'?t browse|cannot browse|unable to browse"
    r"|don'?t have access to|do not have access to"
    r"|can'?t search the (web|internet)|cannot search the (web|internet)"
    r"|knowledge cutoff|training data"
    r"|can'?t provide real-?time|cannot provide real-?time"
    r"|can'?t look up|cannot look up|unable to look up"
    r"|don'?t have the ability|do not have the ability"
    r"|can'?t fetch|cannot fetch|unable to fetch"
    r"|as of my last|as of my knowledge"
    r"|I don'?t have (?:access to )?(?:live|current|real-?time|up-to-date)"
    r")"
)

SELF_CORRECTION_NUDGE = (
    "Stop — you DO have a search_web tool. Use it now to answer the question "
    "with current information. Do not apologize or explain limitations, just "
    "call search_web and answer."
)


def _looks_like_giving_up(text: str) -> bool:
    """Return True if the response text contains give-up phrases."""
    return bool(_GIVE_UP_PATTERNS.search(text))


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
        model_tier: "haiku", "sonnet", or "opus" (ignored for local model, kept for API compat).
        max_tool_rounds: Max number of tool-use rounds before forcing a text response.

    Yields:
        Dicts with "type" key: "text", "status", or "result".
    """
    client = get_local_llm()
    system_prompt = build_system_prompt()

    # Inject relevant memories into system prompt (with token budget)
    with trace_span("memory_inject"):
        try:
            from api.services.memory_store import get_memory_store, format_memories_for_prompt
            memory_store = get_memory_store()
            relevant_memories = memory_store.get_relevant_memories(question, limit=5)
            if relevant_memories:
                # Apply token budget: ~400 words ≈ 500 tokens
                budgeted = []
                word_count = 0
                for m in relevant_memories:
                    words = len(m.content.split())
                    if word_count + words > 400:
                        break
                    budgeted.append(m)
                    word_count += words
                if budgeted:
                    memory_text = format_memories_for_prompt(budgeted)
                    system_prompt.append({"type": "text", "text": memory_text})
        except Exception as e:
            logger.warning(f"Failed to load memories: {e}")

    # Build messages array from conversation history
    messages = []
    if conversation_history:
        for msg in conversation_history[-10:]:
            if msg.role in ("user", "assistant") and msg.content:
                messages.append({"role": msg.role, "content": msg.content})

    # Add current user message (with attachments if any)
    user_content = build_message_content(question, attachments)
    messages.append({"role": "user", "content": user_content})

    result = AgentResult(full_text="", model="local")

    def _track_usage(usage: LLMUsage):
        result.total_input_tokens += usage.input_tokens
        result.total_output_tokens += usage.output_tokens
        # Local model has no cost
        result.total_cost_usd = 0.0

    # Strip cache_control from tool definitions (Anthropic-specific)
    tools = []
    for t in TOOL_DEFINITIONS:
        tool_copy = dict(t)
        tool_copy.pop("cache_control", None)
        schema = dict(tool_copy.get("input_schema", {}))
        schema.pop("cache_control", None)
        tool_copy["input_schema"] = schema
        tools.append(tool_copy)

    for round_num in range(1, max_tool_rounds + 1):
        print(f"[agent] Round {round_num}/{max_tool_rounds} starting")

        text_this_round = ""
        tool_use_blocks = []
        usage_this_round = LLMUsage()
        finish_reason = ""

        api_error_fatal = False
        with trace_span(f"llm_api_round_{round_num}"):
            max_api_retries = 2
            for api_attempt in range(max_api_retries + 1):
                try:
                    async for event in client.astream(
                        messages,
                        system=system_prompt,
                        max_tokens=4096,
                        tools=tools,
                    ):
                        if event["type"] == "text":
                            text_this_round += event["content"]
                            yield {"type": "text", "content": event["content"]}
                        elif event["type"] == "tool_calls":
                            tool_use_blocks = openai_tool_calls_to_anthropic(event["calls"])
                        elif event["type"] == "done":
                            usage_this_round = event["usage"]
                            finish_reason = event.get("finish_reason", "")
                    break  # success
                except Exception as e:
                    if api_attempt < max_api_retries and is_retryable_api_error(e):
                        delay = 2 * (2 ** api_attempt)  # 2s, 4s
                        logger.warning(f"Round {round_num} transient error ({e}), retry {api_attempt + 1}/{max_api_retries} in {delay}s")
                        if text_this_round:
                            yield {"type": "self_correction"}
                            text_this_round = ""
                        yield {"type": "status", "message": f"LLM temporarily unavailable, retrying in {delay}s..."}
                        await asyncio.sleep(delay)
                        continue
                    print(f"[agent] Round {round_num} API error: {e}")
                    if result.full_text:
                        yield {"type": "text", "content": f"\n\n(Search interrupted: {e})"}
                    else:
                        yield {"type": "text", "content": f"Sorry, I encountered an error: {e}"}
                    api_error_fatal = True
                    break
        if api_error_fatal:
            break

        _track_usage(usage_this_round)

        # Build assistant content for message history (keep narration text
        # for the LLM context even if we strip it from the user-facing response)
        assistant_content = []
        if text_this_round:
            assistant_content.append({"type": "text", "text": text_this_round})
        for block in tool_use_blocks:
            assistant_content.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })

        tool_names = [b.name for b in tool_use_blocks]
        print(f"[agent] Round {round_num} done: stop={finish_reason}, tools={tool_names}, text={len(text_this_round)}ch")

        # If model produced text AND tool calls, the text is narration ("I need
        # to look up...") — clear it from the user-facing response. The LLM
        # context (assistant_content above) keeps it for continuity.
        if tool_use_blocks and text_this_round.strip():
            yield {"type": "self_correction"}
        else:
            result.full_text += text_this_round

        # If no tool calls, we're done — unless the model is giving up without trying
        if finish_reason != "tool_calls" or not tool_use_blocks:
            if (
                round_num == 1
                and not result.tool_calls_log
                and text_this_round.strip()
                and _looks_like_giving_up(text_this_round)
            ):
                print("[agent] Self-correction triggered: model gave up without using tools")
                yield {"type": "self_correction"}
                result.full_text = ""
                messages.append({"role": "assistant", "content": assistant_content})
                messages.append({"role": "user", "content": SELF_CORRECTION_NUDGE})
                continue
            break

        # Append the assistant message with tool use blocks
        messages.append({"role": "assistant", "content": assistant_content})

        # Execute tools in parallel
        async def _exec_one(block):
            name = block.name
            logger.info(f"Executing tool: {name} with input: {block.input}")
            with trace_span(f"tool_{name}"):
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
        # Exhausted all tool rounds — force a final synthesis round without tools.
        # Add an explicit instruction so the LLM knows to produce a text answer
        # instead of trying to call more tools.
        print("[agent] Exhausted tool rounds, running synthesis round")
        messages.append({
            "role": "user",
            "content": (
                "You have finished gathering information. Now answer the original "
                "question based on everything you found above. Do not call any more "
                "tools — just provide your answer in plain text."
            ),
        })
        try:
            synthesis_events = 0
            async for event in client.astream(
                messages,
                system=system_prompt,
                max_tokens=4096,
                timeout=180,
            ):
                synthesis_events += 1
                if event["type"] == "text":
                    result.full_text += event["content"]
                    yield {"type": "text", "content": event["content"]}
                elif event["type"] == "tool_calls":
                    # LLM tried to call tools despite no tools in request —
                    # log and ignore (the text, if any, was already captured)
                    print(f"[agent] Synthesis round produced tool_calls (ignored): {[c.get('function', {}).get('name', '?') for c in event.get('calls', [])]}")
                elif event["type"] == "done":
                    _track_usage(event["usage"])
                    print(f"[agent] Synthesis round done: finish_reason={event.get('finish_reason', '?')}, events={synthesis_events}")
        except Exception as e:
            error_msg = str(e) or f"{type(e).__name__} (no message)"
            print(f"[agent] Synthesis round error: {error_msg}")
            yield {"type": "text", "content": f"\n\n(Error during synthesis: {error_msg})"}

    # If we ran tools but still ended up with no text, construct a fallback
    # from tool results so the user gets something useful.
    if not result.full_text.strip() and result.tool_calls_log:
        # Clear any whitespace-only content that was already streamed
        if result.full_text:
            yield {"type": "self_correction"}
            result.full_text = ""
        # Exclude sensitive tools from raw fallback output
        _SENSITIVE_TOOLS = {"get_message_history", "search_email"}
        non_error_results = [
            tc["result_preview"]
            for tc in result.tool_calls_log
            if not tc.get("is_error")
            and tc["tool"] not in _SENSITIVE_TOOLS
            and tc.get("result_preview", "").strip()
        ]
        if non_error_results:
            fallback = "Here's what I found:\n\n" + "\n\n".join(non_error_results)
        else:
            fallback = "I searched but couldn't find relevant information to answer your question."
        result.full_text = fallback
        yield {"type": "text", "content": fallback}
        print(f"[agent] Used fallback response ({len(fallback)}ch)")

    print(f"[agent] Loop complete: {len(result.tool_calls_log)} tool calls, {len(result.full_text)}ch text")
    # Yield the final result
    yield {"type": "result", "result": result}
