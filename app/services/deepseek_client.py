"""
Voice Agent Platform — DeepSeek AI Client

Wraps the OpenAI-compatible DeepSeek API for chat completions with function calling.
Uses the `openai` Python SDK with DeepSeek's base_url.

Features:
- Async chat completions with tool/function calling support
- Automatic retry with exponential backoff (3 attempts)
- Request timeout handling (30s default)
- Structured error handling with logging
- Token usage tracking

Usage:
    from app.services.deepseek_client import DeepSeekClient

    client = DeepSeekClient()
    response = await client.chat_completion(
        messages=[{"role": "user", "content": "What's on the menu?"}],
        tools=TOOL_DEFINITIONS,
    )
"""

import asyncio
from typing import Any

from openai import AsyncOpenAI, APIConnectionError, APITimeoutError, RateLimitError

from app.config import get_settings
from app.logger import get_logger

logger = get_logger(__name__)

# Retry configuration
MAX_RETRIES = 3
BASE_RETRY_DELAY = 1.0  # seconds
REQUEST_TIMEOUT = 30.0  # seconds


class DeepSeekError(Exception):
    """Base exception for DeepSeek API errors."""

    def __init__(self, message: str, original_error: Exception | None = None):
        super().__init__(message)
        self.original_error = original_error


class DeepSeekClient:
    """
    Async client for DeepSeek's OpenAI-compatible Chat Completion API.

    Handles connection setup, retries, timeouts, and error logging.
    """

    def __init__(self):
        settings = get_settings()
        self.model = settings.DEEPSEEK_MODEL

        self.client = AsyncOpenAI(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            timeout=REQUEST_TIMEOUT,
        )

        logger.info(
            "deepseek_client_initialized",
            model=self.model,
            base_url=settings.DEEPSEEK_BASE_URL,
        )

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """
        Send a chat completion request to DeepSeek with optional function calling.

        Args:
            messages: Conversation history [{role, content}].
            tools: Function tool definitions for DeepSeek to choose from.
            tool_choice: "auto" (let AI decide), "none" (no tools), or specific tool.
            temperature: Creativity level (0.0 = deterministic, 1.0 = creative).
            max_tokens: Maximum tokens in the response.

        Returns:
            Dict with keys:
                - message: The assistant's response message object.
                - tool_calls: List of tool calls if any (or empty list).
                - content: Text content of the response (or None if tool call).
                - usage: Token usage stats {prompt_tokens, completion_tokens, total_tokens}.

        Raises:
            DeepSeekError: On API failure after all retries exhausted.
        """
        last_error = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await self._make_request(
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    attempt=attempt,
                )

            except RateLimitError as e:
                last_error = e
                wait_time = BASE_RETRY_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "deepseek_rate_limited",
                    attempt=attempt,
                    max_retries=MAX_RETRIES,
                    retry_after_seconds=wait_time,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(wait_time)

            except APITimeoutError as e:
                last_error = e
                logger.warning(
                    "deepseek_timeout",
                    attempt=attempt,
                    max_retries=MAX_RETRIES,
                    timeout_seconds=REQUEST_TIMEOUT,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(BASE_RETRY_DELAY)

            except APIConnectionError as e:
                last_error = e
                logger.warning(
                    "deepseek_connection_error",
                    attempt=attempt,
                    max_retries=MAX_RETRIES,
                    error=str(e),
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(BASE_RETRY_DELAY)

            except Exception as e:
                # Non-retryable errors (auth, invalid request, etc.)
                logger.error(
                    "deepseek_api_error",
                    attempt=attempt,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                raise DeepSeekError(
                    f"DeepSeek API error: {str(e)}", original_error=e
                ) from e

        # All retries exhausted
        logger.error(
            "deepseek_retries_exhausted",
            max_retries=MAX_RETRIES,
            last_error=str(last_error),
        )
        raise DeepSeekError(
            f"DeepSeek API failed after {MAX_RETRIES} attempts: {str(last_error)}",
            original_error=last_error,
        )

    async def _make_request(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str,
        temperature: float,
        max_tokens: int,
        attempt: int,
    ) -> dict[str, Any]:
        """Execute a single chat completion request."""
        # Build request kwargs
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # Only include tools if provided
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        logger.debug(
            "deepseek_request",
            attempt=attempt,
            model=self.model,
            message_count=len(messages),
            has_tools=bool(tools),
        )

        response = await self.client.chat.completions.create(**kwargs)

        # Extract response data
        choice = response.choices[0]
        message = choice.message

        # Parse tool calls if present
        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "function_name": tc.function.name,
                    "arguments": tc.function.arguments,  # JSON string
                })

        result = {
            "message": {
                "role": message.role,
                "content": message.content,
                "tool_calls": message.tool_calls,
            },
            "tool_calls": tool_calls,
            "content": message.content,
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            },
            "finish_reason": choice.finish_reason,
        }

        logger.info(
            "deepseek_response",
            attempt=attempt,
            finish_reason=choice.finish_reason,
            has_tool_calls=bool(tool_calls),
            tool_count=len(tool_calls),
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
        )

        return result

    async def health_check(self) -> bool:
        """
        Verify DeepSeek API connectivity with a minimal request.

        Returns:
            True if API is reachable and responding, False otherwise.
        """
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
            )
            return bool(response.choices)
        except Exception as e:
            logger.error("deepseek_health_check_failed", error=str(e))
            return False
