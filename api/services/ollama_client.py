"""
Ollama Client for Local LLM inference.

Connects to a local Ollama server for query routing, fact filtering, and validation.

Enhanced for the multi-stage fact extraction pipeline:
- Stage 1: Filter interactions (local, fast)
- Stage 3: Validate facts and assign confidence (local)
"""
import asyncio
import json
import logging
import re
import httpx
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)


class OllamaError(Exception):
    """Error communicating with Ollama."""
    pass


class OllamaClient:
    """
    Client for the Ollama local LLM API.

    Provides async inference for:
    - Query routing decisions
    - Fact extraction filtering (Stage 1)
    - Fact validation and confidence scoring (Stage 3)
    """

    # Default settings for fact extraction
    DEFAULT_TIMEOUT = 30  # Longer timeout for batch processing
    MAX_RETRIES = 3
    RETRY_BACKOFF_BASE = 2  # Exponential backoff multiplier

    def __init__(
        self,
        host: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None
    ):
        """
        Initialize Ollama client.

        Args:
            host: Ollama server URL (default from settings)
            model: Model name to use (default from settings)
            timeout: Request timeout in seconds (default from settings)
        """
        self.host = host or settings.ollama_host
        self.model = model or settings.ollama_model
        self.timeout = timeout or settings.ollama_timeout

    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        timeout: Optional[int] = None
    ) -> str:
        """
        Generate a response from the local LLM with retry logic.

        Args:
            prompt: The prompt to send to the model
            model: Model to use (defaults to instance model)
            temperature: Sampling temperature (default 0.3 for fact extraction)
            max_tokens: Maximum tokens to generate
            timeout: Request timeout (defaults to instance timeout)

        Returns:
            The model's response text

        Raises:
            OllamaError: If communication fails after retries
        """
        url = f"{self.host}/api/generate"
        payload = {
            "model": model or self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            }
        }

        request_timeout = timeout or self.timeout
        last_error = None

        for attempt in range(self.MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=request_timeout) as client:
                    response = await client.post(url, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    return data.get("response", "")

            except httpx.TimeoutException as e:
                last_error = OllamaError(f"Timeout connecting to Ollama: {e}")
                logger.warning(f"Ollama timeout (attempt {attempt + 1}/{self.MAX_RETRIES})")
            except httpx.ConnectError as e:
                last_error = OllamaError(f"Connection error to Ollama: {e}")
                logger.warning(f"Ollama connection error (attempt {attempt + 1}/{self.MAX_RETRIES})")
            except httpx.HTTPStatusError as e:
                last_error = OllamaError(f"HTTP error from Ollama: {e}")
                logger.warning(f"Ollama HTTP error (attempt {attempt + 1}/{self.MAX_RETRIES}): {e}")
            except Exception as e:
                last_error = OllamaError(f"Error communicating with Ollama: {e}")
                logger.warning(f"Ollama error (attempt {attempt + 1}/{self.MAX_RETRIES}): {e}")

            # Exponential backoff before retry
            if attempt < self.MAX_RETRIES - 1:
                wait_time = self.RETRY_BACKOFF_BASE ** attempt
                await asyncio.sleep(wait_time)

        raise last_error

    async def generate_json(
        self,
        prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        timeout: Optional[int] = None
    ) -> dict:
        """
        Generate and parse a JSON response from the local LLM.

        Handles common response formats:
        - Raw JSON
        - JSON wrapped in markdown code blocks
        - JSON with surrounding text

        Args:
            prompt: The prompt (should request JSON output)
            model: Model to use
            temperature: Lower temperature for structured output
            max_tokens: Maximum tokens to generate
            timeout: Request timeout

        Returns:
            Parsed JSON as a dict

        Raises:
            OllamaError: If generation or JSON parsing fails
        """
        response_text = await self.generate(
            prompt=prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout
        )

        return self._extract_json(response_text)

    def _extract_json(self, text: str) -> dict:
        """
        Extract JSON from LLM response text.

        Handles:
        - Raw JSON
        - ```json ... ``` code blocks
        - ``` ... ``` code blocks
        - JSON embedded in other text

        Args:
            text: Raw response text

        Returns:
            Parsed JSON dict

        Raises:
            OllamaError: If no valid JSON found
        """
        # Try raw JSON first
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass

        # Try markdown JSON code block
        json_block_match = re.search(r'```json\s*([\s\S]*?)\s*```', text)
        if json_block_match:
            try:
                return json.loads(json_block_match.group(1))
            except json.JSONDecodeError:
                pass

        # Try any code block
        code_block_match = re.search(r'```\s*([\s\S]*?)\s*```', text)
        if code_block_match:
            try:
                return json.loads(code_block_match.group(1))
            except json.JSONDecodeError:
                pass

        # Try to find JSON object in text (find { and match to closing })
        brace_start = text.find('{')
        if brace_start >= 0:
            # Find matching closing brace
            depth = 0
            for i, char in enumerate(text[brace_start:], brace_start):
                if char == '{':
                    depth += 1
                elif char == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[brace_start:i + 1])
                        except json.JSONDecodeError:
                            pass
                        break

        raise OllamaError(f"Failed to extract JSON from response: {text[:200]}...")

    def is_available(self) -> bool:
        """
        Check if Ollama server is available and model is loaded.

        Uses /api/tags endpoint to verify the server is running
        and the configured model is available.

        Returns:
            True if Ollama is running and model is available
        """
        try:
            response = httpx.get(f"{self.host}/api/tags", timeout=2.0)
            if response.status_code != 200:
                # Track Ollama as unavailable (but not critical - has fallbacks)
                from api.services.service_health import mark_service_failed, Severity
                mark_service_failed("ollama", f"HTTP {response.status_code}", Severity.WARNING)
                return False

            data = response.json()
            models = data.get("models", [])

            # Check if our model is in the list
            model_names = [m.get("name", "") for m in models]

            # Handle model name variations (e.g., "llama3.2:3b" vs "llama3.2:3b-instruct")
            for name in model_names:
                if self.model in name or name.startswith(self.model.split(":")[0]):
                    # Mark Ollama as healthy
                    from api.services.service_health import mark_service_healthy
                    mark_service_healthy("ollama")
                    return True

            # If specific model not found, still return True if server is up
            # (model can be pulled on first use)
            logger.warning(f"Model {self.model} not found in Ollama. Available: {model_names}")
            if len(models) > 0:
                from api.services.service_health import mark_service_healthy
                mark_service_healthy("ollama")
            return len(models) > 0

        except Exception as e:
            logger.debug(f"Ollama availability check failed: {e}")
            # Track Ollama as unavailable
            from api.services.service_health import mark_service_failed, Severity
            mark_service_failed("ollama", str(e), Severity.WARNING)
            return False

    async def is_available_async(self) -> bool:
        """
        Async version of availability check.

        Returns:
            True if Ollama is running and model is available
        """
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{self.host}/api/tags")
                if response.status_code != 200:
                    return False

                data = response.json()
                models = data.get("models", [])
                return len(models) > 0

        except Exception:
            return False
