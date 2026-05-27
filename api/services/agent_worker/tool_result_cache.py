"""Per-session tool result cache for the LifeOS MCP HTTP server (#139 §4).

When an agent calls the same tool twice within a single session with identical
arguments — e.g., `lifeos_calendar_upcoming` early to set context and again
later to confirm — the second result is identical work. Caching it MCP-side
saves the round-trip and the per-turn input cost of having the tool result
re-processed by the agent.

Design choices:
- **Key**: `(session_id, tool_name, sorted_args_json)`. Sorting args keys
  ensures `{"a": 1, "b": 2}` and `{"b": 2, "a": 1}` collide as expected.
- **TTL**: uniform 60 seconds. The cache only fires on identical-args repeats
  within a single session; per-tool tuning would add complexity for marginal
  gain.
- **Capacity**: 100 entries total, evicted oldest-first when full. Bounds
  worst-case memory regardless of session count.
- **Per-session clear**: `clear_session(session_id)` drops all entries for a
  terminated session. Cheap because the cache is small.

Thread-safety: the underlying ordered-dict mutation isn't atomic, but the
worker process is single-threaded for MCP handling and the FastAPI handler
is async-but-cooperative. A lock would add cost without buying anything.
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from typing import Any


DEFAULT_TTL_SECONDS = 60
DEFAULT_MAX_ENTRIES = 100


class SessionToolResultCache:
    """In-process LRU+TTL cache keyed on (session_id, tool_name, args)."""

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: callable = time.time,
    ):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        # OrderedDict preserves insertion order; move_to_end on hit gives LRU.
        # Value shape: (stored_at, result_dict)
        self._store: OrderedDict[tuple[str, str, str], tuple[float, dict]] = OrderedDict()

    @staticmethod
    def _key(session_id: str, tool_name: str, args: dict) -> tuple[str, str, str]:
        """Build the cache key. Args are JSON-canonicalized so kwarg
        reordering doesn't bypass the cache."""
        try:
            args_json = json.dumps(args or {}, sort_keys=True, default=str)
        except Exception:
            args_json = repr(args)
        return (session_id, tool_name, args_json)

    def get(self, session_id: str, tool_name: str, args: dict) -> Any | None:
        """Return a cached result if present and fresh, else None."""
        if not session_id:
            return None
        key = self._key(session_id, tool_name, args)
        entry = self._store.get(key)
        if entry is None:
            return None
        stored_at, result = entry
        if self._clock() - stored_at > self.ttl_seconds:
            # Stale — drop it so the next put can take the slot.
            self._store.pop(key, None)
            return None
        # LRU touch.
        self._store.move_to_end(key)
        return result

    def put(self, session_id: str, tool_name: str, args: dict, result: Any) -> None:
        """Store a fresh result. Evicts oldest entry when capacity is hit."""
        if not session_id:
            return
        key = self._key(session_id, tool_name, args)
        self._store[key] = (self._clock(), result)
        self._store.move_to_end(key)
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)

    def clear_session(self, session_id: str) -> int:
        """Drop all entries for a terminated session. Returns count cleared."""
        keys_to_drop = [k for k in self._store if k[0] == session_id]
        for k in keys_to_drop:
            self._store.pop(k, None)
        return len(keys_to_drop)

    def __len__(self) -> int:
        return len(self._store)
