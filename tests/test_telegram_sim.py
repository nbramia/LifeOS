#!/usr/bin/env python3
"""
Simulate a Telegram message through the LifeOS chat pipeline.

Hits the same /api/ask/stream endpoint that chat_via_api uses,
showing all SSE events in real time and the final answer Telegram would send.

Usage:
    ~/.venvs/lifeos/bin/python tests/test_telegram_sim.py "Whats Tays KTN?"
    ~/.venvs/lifeos/bin/python tests/test_telegram_sim.py "what's the model of the smith skiing helmet I bought?"
"""
import asyncio
import json
import sys
import time

import httpx


async def simulate_telegram(question: str, port: int = 8000):
    """Replicate chat_via_api behavior, printing every SSE event."""
    body = {"question": question}
    full_text = ""
    conv_id = None
    start = time.time()

    print(f"\n{'='*60}")
    print(f"TELEGRAM SIM: {question}")
    print(f"{'='*60}\n")

    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream(
            "POST",
            f"http://localhost:{port}/api/ask/stream",
            json=body,
        ) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    print(f"  [BAD JSON] {line}")
                    continue

                etype = event.get("type", "?")
                elapsed = time.time() - start

                if etype == "content":
                    chunk = event.get("content", "")
                    full_text += chunk
                    # Print content chunks inline (no newline per chunk)
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                elif etype == "conversation_id":
                    conv_id = event.get("conversation_id")
                    print(f"  [{elapsed:5.1f}s] conversation_id: {conv_id}")
                elif etype == "routing":
                    print(f"  [{elapsed:5.1f}s] routing: {event.get('sources')} - {event.get('reasoning')}")
                elif etype == "status":
                    print(f"  [{elapsed:5.1f}s] status: {event.get('message')}")
                elif etype == "usage":
                    cost = event.get("cost_usd", 0)
                    inp = event.get("input_tokens", 0)
                    out = event.get("output_tokens", 0)
                    model = event.get("model", "?")
                    print(f"\n  [{elapsed:5.1f}s] usage: {inp} in / {out} out = ${cost:.4f} ({model})")
                elif etype == "sources":
                    sources = event.get("sources", [])
                    print(f"  [{elapsed:5.1f}s] sources: {len(sources)} tools used")
                    for s in sources:
                        print(f"    - {s.get('file_name', '?')}")
                elif etype == "error":
                    print(f"\n  [{elapsed:5.1f}s] ERROR: {event.get('message')}")
                elif etype == "done":
                    pass
                elif etype == "code_intent":
                    print(f"  [{elapsed:5.1f}s] code_intent: {event.get('task')}")
                else:
                    print(f"  [{elapsed:5.1f}s] {etype}: {json.dumps(event)[:200]}")

    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print(f"TOTAL TIME: {elapsed:.1f}s")
    print(f"ANSWER LENGTH: {len(full_text)} chars")
    print(f"{'='*60}")
    print(f"\nFINAL ANSWER (what Telegram sends):")
    print(f"{'─'*40}")
    print(full_text or "(empty)")
    print(f"{'─'*40}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python tests/test_telegram_sim.py \"your question here\"")
        sys.exit(1)
    asyncio.run(simulate_telegram(sys.argv[1]))
