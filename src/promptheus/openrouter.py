"""Streaming chat completions against a single model."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, TypedDict

import httpx

from promptheus.config import Settings

logger = logging.getLogger(__name__)

DONE_SENTINEL = "[DONE]"

Role = Literal["system", "user", "assistant"]


class Message(TypedDict):
    role: Role
    content: str


@dataclass(frozen=True)
class TextDelta:
    """A fragment of the answer."""

    text: str


@dataclass(frozen=True)
class ReasoningDelta:
    """A fragment of the model's reasoning, kept apart from the answer.

    Reasoning models emit these with `content` empty for as long as they think
    — measured at several seconds, and longer under load. Dropping them leaves
    the column blank while the model works, and a run capped at a low
    `max_tokens` can finish having produced nothing but reasoning. Keeping it
    separate lets the interface show progress without mixing thinking into the
    answer being compared.
    """

    text: str


@dataclass(frozen=True)
class Usage:
    """Real token counts, reported by OpenRouter once the answer is complete."""

    prompt_tokens: int
    completion_tokens: int
    cost: Decimal | None = None


StreamEvent = TextDelta | ReasoningDelta | Usage


class CompletionError(Exception):
    """A completion failed.

    Always carries the model id: the caller is running this against N models at
    once and a bare message would not say which column died.
    """

    def __init__(self, model_id: str, message: str) -> None:
        self.model_id = model_id
        self.message = message
        super().__init__(f"{model_id}: {message}")


@dataclass(frozen=True)
class RoutingOptions:
    """Provider routing, sent as the `provider` object.

    Every field defaults to OpenRouter's own behaviour, and `as_payload`
    returns an empty dict in that case so the key is omitted entirely rather
    than pinning us to today's defaults.
    """

    sort: Literal["price", "throughput", "latency"] | None = None
    order: tuple[str, ...] = ()
    allow_fallbacks: bool = True
    data_collection: Literal["allow", "deny"] = "allow"

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.sort is not None:
            payload["sort"] = self.sort
        if self.order:
            payload["order"] = list(self.order)
        if not self.allow_fallbacks:
            payload["allow_fallbacks"] = False
        if self.data_collection == "deny":
            payload["data_collection"] = "deny"
        return payload


class OpenRouterClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http

    async def stream_completion(
        self,
        model_id: str,
        messages: Sequence[Message],
        routing: RoutingOptions | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream one model's answer.

        Yields `TextDelta` as the answer arrives and a final `Usage` when
        OpenRouter reports it. Raises `CompletionError` for anything that goes
        wrong, including errors that arrive mid-stream.

        Deliberately does not retry: a partially streamed answer cannot be
        retried without showing the reader duplicated text.
        """
        key = self._settings.openrouter_api_key
        if key is None:
            raise CompletionError(model_id, "no OPENROUTER_API_KEY configured")

        body: dict[str, Any] = {
            "model": model_id,
            "messages": [dict(message) for message in messages],
            "stream": True,
            # Ask for real token counts, so the plan-04 estimate can be
            # replaced by the actual figure once the run finishes.
            "usage": {"include": True},
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if routing is not None and (provider := routing.as_payload()):
            body["provider"] = provider

        headers = {
            "Authorization": f"Bearer {key.get_secret_value()}",
            "HTTP-Referer": self._settings.openrouter_app_url,
            "X-OpenRouter-Title": self._settings.openrouter_app_title,
            "Content-Type": "application/json",
        }

        timeout = httpx.Timeout(
            connect=30.0,
            # Generous on purpose: a reasoning model can think for minutes
            # before it emits a single token.
            read=self._settings.request_timeout_seconds,
            write=60.0,
            pool=30.0,
        )

        try:
            async with self._http.stream(
                "POST",
                f"{self._settings.openrouter_base_url}/chat/completions",
                headers=headers,
                json=body,
                timeout=timeout,
            ) as response:
                if response.status_code >= httpx.codes.BAD_REQUEST:
                    await response.aread()
                    raise CompletionError(model_id, _describe_http_error(response))

                async for line in response.aiter_lines():
                    for event in _parse_line(model_id, line):
                        yield event
                    if _is_done(line):
                        return
        except httpx.HTTPError as error:
            raise CompletionError(model_id, f"transport error: {error}") from error


def _is_done(line: str) -> bool:
    return line.strip() == f"data: {DONE_SENTINEL}"


def _parse_line(model_id: str, raw_line: str) -> list[StreamEvent]:
    line = raw_line.strip()

    if not line:
        return []

    # OpenRouter sends `: OPENROUTER PROCESSING` comment lines as keep-alives
    # while a slow model thinks. Parsing them as JSON is the single most
    # likely way to break this reader.
    if line.startswith(":"):
        return []

    if not line.startswith("data:"):
        return []

    payload = line[len("data:") :].strip()
    if payload == DONE_SENTINEL:
        return []

    try:
        chunk: Any = json.loads(payload)
    except json.JSONDecodeError:
        # A malformed chunk should cost one fragment, not the whole answer.
        logger.warning("skipping unparseable chunk from %s: %r", model_id, payload[:200])
        return []

    if not isinstance(chunk, dict):
        return []

    # An error can arrive after a perfectly healthy start, so this is checked
    # on every chunk rather than only at HTTP-status time.
    if error := chunk.get("error"):
        raise CompletionError(model_id, _describe_chunk_error(error))

    events: list[StreamEvent] = []

    for choice in chunk.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        # Reasoning first: it is emitted while `content` is still empty, so
        # this is the order the reader actually sees them in.
        reasoning = delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            events.append(ReasoningDelta(reasoning))
        content = delta.get("content")
        if isinstance(content, str) and content:
            events.append(TextDelta(content))

    if usage := chunk.get("usage"):
        parsed = _parse_usage(usage)
        if parsed is not None:
            events.append(parsed)

    return events


def _parse_usage(usage: Any) -> Usage | None:
    if not isinstance(usage, dict):
        return None
    cost_value = usage.get("cost")
    cost: Decimal | None = None
    if cost_value is not None:
        try:
            # Via str: the value arrives as a JSON number, and going through
            # float would reintroduce the imprecision plan 04 avoids.
            cost = Decimal(str(cost_value))
        except (InvalidOperation, ValueError):
            cost = None
    return Usage(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        cost=cost,
    )


def _describe_http_error(response: httpx.Response) -> str:
    try:
        payload: Any = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:200]}"
    if isinstance(payload, dict) and (error := payload.get("error")):
        return f"HTTP {response.status_code}: {_describe_chunk_error(error)}"
    return f"HTTP {response.status_code}"


def _describe_chunk_error(error: Any) -> str:
    if isinstance(error, dict):
        message = error.get("message") or "unknown error"
        code = error.get("code")
        return f"{message} (code {code})" if code is not None else str(message)
    return str(error)
