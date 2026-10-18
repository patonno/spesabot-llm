"""Async Gemini client for multimodal voice note parsing."""

import asyncio
import json
import logging
from typing import Any

from google import genai
from google.genai import errors, types

from actions import validate_actions
from prompts import ACTIONS_SCHEMA, SYSTEM_PROMPT, sanitize_actions

logger = logging.getLogger(__name__)


class GeminiError(Exception):
    """Base exception for Gemini operations."""


class GeminiRateLimitError(GeminiError):
    """Raised when Gemini quota or rate limit is exhausted (HTTP 429)."""


class GeminiServiceError(GeminiError):
    """Raised when Gemini service encounters transient or connection issues."""


class GeminiParseError(GeminiError):
    """Raised when Gemini output is missing or cannot be parsed as valid JSON."""


class GeminiClient:
    """Async wrapper around Google GenAI client for speech-to-actions processing."""

    def __init__(self, api_key: str, model: str = "gemini-3.8-flash", timeout_seconds: float = 30.0):
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._client: genai.Client | None = genai.Client(api_key=api_key)

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            raise GeminiServiceError("Gemini client is closed")
        return self._client

    async def aclose(self) -> None:
        """Close asynchronous client resources."""
        if self._client is not None:
            client = self._client
            self._client = None
            try:
                if hasattr(client, "aio") and hasattr(client.aio, "aclose"):
                    await client.aio.aclose()
            except Exception as exc:
                logger.debug("Error closing GenAI client: %s", exc)

    async def __aenter__(self) -> "GeminiClient":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()

    async def parse_voice_note(self, audio_bytes: bytes, mime_type: str = "audio/ogg") -> dict:
        """Send audio to Gemini and return validated JSON actions envelope."""
        part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
        contents = [part, "Estrai le azioni per la lista della spesa dal messaggio vocale."]
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=ACTIONS_SCHEMA,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        attempts = 3
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    response = await self.client.aio.models.generate_content(
                        model=self.model,
                        contents=contents,
                        config=config,
                    )
                return self._extract_and_validate(response)
            except errors.APIError as exc:
                if exc.code == 429 or "RESOURCE_EXHAUSTED" in str(exc) or "429" in str(exc):
                    logger.warning("Gemini rate limit encountered: %s", type(exc).__name__)
                    raise GeminiRateLimitError("Rate limit exceeded") from exc
                if isinstance(exc, errors.ServerError) or (exc.code and exc.code >= 500):
                    last_error = exc
                    if attempt < attempts:
                        logger.info("Transient 5xx error from Gemini, retrying: %s", type(exc).__name__)
                        await asyncio.sleep(1.5)
                        continue
                    raise GeminiServiceError("Gemini service unavailable") from exc
                raise GeminiServiceError(f"Gemini API error: {type(exc).__name__}") from exc
            except (TimeoutError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < attempts:
                    logger.info("Gemini request timed out, retrying once...")
                    await asyncio.sleep(1.5)
                    continue
                raise GeminiServiceError("Gemini request timed out") from exc
            except GeminiParseError:
                raise
            except GeminiError:
                raise
            except Exception as exc:
                logger.error("Unexpected error communicating with Gemini: %s", type(exc).__name__)
                raise GeminiServiceError(f"Unexpected Gemini failure: {type(exc).__name__}") from exc

        if last_error:
            raise GeminiServiceError("Gemini request failed after retry") from last_error
        raise GeminiServiceError("Unknown failure calling Gemini")

    def _extract_and_validate(self, response: Any) -> dict:
        """Parse raw model response into validated actions envelope."""
        text = getattr(response, "text", None)
        if not text or not isinstance(text, str) or not text.strip():
            # Check candidates/parts if text property is empty
            try:
                candidates = getattr(response, "candidates", None)
                if candidates and candidates[0].content and candidates[0].content.parts:
                    text = candidates[0].content.parts[0].text
            except (IndexError, AttributeError):
                text = None

        if not text or not text.strip():
            raise GeminiParseError("Empty response from Gemini")

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GeminiParseError(f"Malformed JSON from Gemini: {exc}") from exc

        cleaned = sanitize_actions(payload)
        try:
            validate_actions(cleaned)
        except ValueError as exc:
            raise GeminiParseError(f"Invalid actions envelope: {exc}") from exc

        return cleaned
