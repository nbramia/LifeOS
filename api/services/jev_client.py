"""Client for TypeSafe's Jev typed-judgment API (https://docs.typesafe.ai).

Jev answers a batch of typed questions (`choice`/`score`/`noul`) about a
piece of `state` with calibrated probabilities, in one call, instead of a
freeform generative completion parsed with regex. It's a plain `httpx`
wrapper: callers check `jev_configured()` before constructing a client or
making a Jev-backed judgment.

Nothing in this module logs the `state` or `questions` passed to `ask()`/
`aask()`, at any log level, in any path including errors.
"""
import asyncio
import logging
import time
from typing import Any

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

_ENDPOINT_PATH = "/v1/systemone"
_MAX_ATTEMPTS = 3


class JevError(Exception):
    """Raised when a Jev call fails: a non-2xx response (after retries on
    429) or a response body without a usable `answers` field. The message
    carries only the HTTP status and a short reason — never the request
    `state` or `questions`."""


def jev_configured() -> bool:
    """True once a TypeSafe API key is set. Mirrors `Settings.remote_llm_configured`'s
    "configured" convention (config/settings.py) — callers check this before
    constructing a JevClient or making any Jev-backed judgment."""
    return settings.jev_configured


class JevClient:
    """Thin `httpx` wrapper around TypeSafe's Jev `/v1/systemone` endpoint.

    `transport` lets callers (and tests) inject an `httpx.MockTransport`
    instead of making a real request. `last_usage` and `last_model` reflect
    the most recent successful call.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = "https://api.typesafe.ai",
        model: str = "jev-1.13.0",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.api_key = api_key if api_key is not None else settings.typesafe_api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._transport = transport
        self.last_usage: int | None = None
        self.last_model: str | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _body(self, state: Any, questions: dict, model: str | None) -> dict:
        return {
            "state": state,
            "model": model or self.model,
            "questions": questions,
        }

    def _retry_delay(self, attempt: int, response: httpx.Response) -> float:
        """`retry-after` (seconds) when the response carries one, else
        exponential backoff: 1s, 2s, 4s for attempts 0, 1, 2."""
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return float(2 ** attempt)

    def _parse_response(self, response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            raise JevError(f"Jev response body was not JSON (status {response.status_code})")
        answers = body.get("answers") if isinstance(body, dict) else None
        if not isinstance(answers, dict):
            raise JevError(f"Jev response missing 'answers' (status {response.status_code})")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        self.last_usage = usage.get("input_tokens")
        self.last_model = body.get("model")
        return answers

    def ask(self, state: Any, questions: dict, *, model: str | None = None) -> dict:
        """Synchronous POST to `/v1/systemone`. Returns the `answers` dict.

        Retries up to 3 total attempts on a 429 response, honoring
        `retry-after` when present, else exponential backoff. Raises
        `JevError` on any other non-2xx status, on a response body without
        an `answers` field, once 429 retries are exhausted, on a
        transport-level failure (timeout, connection error), or immediately,
        without sending a request, if no API key is set.
        """
        if not self.api_key:
            raise JevError("Jev is not configured: no API key")
        body = self._body(state, questions, model)
        with httpx.Client(transport=self._transport, timeout=self.timeout) as client:
            for attempt in range(_MAX_ATTEMPTS):
                try:
                    response = client.post(
                        f"{self.base_url}{_ENDPOINT_PATH}", json=body, headers=self._headers()
                    )
                except httpx.HTTPError as exc:
                    raise JevError(f"Jev request failed: {type(exc).__name__}") from exc
                if response.status_code == 429:
                    if attempt < _MAX_ATTEMPTS - 1:
                        logger.warning(
                            "Jev request rate-limited (attempt %d/%d), retrying",
                            attempt + 1, _MAX_ATTEMPTS,
                        )
                        time.sleep(self._retry_delay(attempt, response))
                        continue
                    raise JevError(f"Jev request failed: status 429 after {_MAX_ATTEMPTS} attempts")
                if response.status_code // 100 != 2:
                    raise JevError(f"Jev request failed: status {response.status_code}")
                return self._parse_response(response)
        raise JevError("Jev request failed: exhausted retries")  # pragma: no cover

    async def aask(self, state: Any, questions: dict, *, model: str | None = None) -> dict:
        """Async counterpart to `ask()`. Same retry and error semantics."""
        if not self.api_key:
            raise JevError("Jev is not configured: no API key")
        body = self._body(state, questions, model)
        async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout) as client:
            for attempt in range(_MAX_ATTEMPTS):
                try:
                    response = await client.post(
                        f"{self.base_url}{_ENDPOINT_PATH}", json=body, headers=self._headers()
                    )
                except httpx.HTTPError as exc:
                    raise JevError(f"Jev request failed: {type(exc).__name__}") from exc
                if response.status_code == 429:
                    if attempt < _MAX_ATTEMPTS - 1:
                        logger.warning(
                            "Jev request rate-limited (attempt %d/%d), retrying",
                            attempt + 1, _MAX_ATTEMPTS,
                        )
                        await asyncio.sleep(self._retry_delay(attempt, response))
                        continue
                    raise JevError(f"Jev request failed: status 429 after {_MAX_ATTEMPTS} attempts")
                if response.status_code // 100 != 2:
                    raise JevError(f"Jev request failed: status {response.status_code}")
                return self._parse_response(response)
        raise JevError("Jev request failed: exhausted retries")  # pragma: no cover
