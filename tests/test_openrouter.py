"""Tests for the streaming client. Nothing here touches the network."""

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from promptheus.config import Settings
from promptheus.openrouter import (
    CompletionError,
    Message,
    OpenRouterClient,
    ReasoningDelta,
    RoutingOptions,
    StreamEvent,
    TextDelta,
    Usage,
)

BASE_URL = "https://openrouter.test/api/v1"
COMPLETIONS_URL = f"{BASE_URL}/chat/completions"
MODEL = "vendor/model"
MESSAGES: list[Message] = [{"role": "user", "content": "hello"}]


def make_settings(api_key: str | None = "sk-test-key") -> Settings:
    return Settings(
        openrouter_api_key=SecretStr(api_key) if api_key else None,
        openrouter_base_url=BASE_URL,
        openrouter_app_url="http://localhost:8000",
        openrouter_app_title="Promptheus",
    )


def delta_chunk(text: str) -> str:
    return json.dumps({"choices": [{"delta": {"content": text}}]})


def sse(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def sse_response(*lines: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=sse(*lines),
        headers={"content-type": "text/event-stream"},
    )


async def collect(
    response: httpx.Response,
    settings: Settings | None = None,
    routing: RoutingOptions | None = None,
    max_tokens: int | None = None,
) -> list[StreamEvent]:
    respx.post(COMPLETIONS_URL).mock(return_value=response)
    async with httpx.AsyncClient() as http:
        client = OpenRouterClient(settings or make_settings(), http)
        return [
            event
            async for event in client.stream_completion(
                MODEL, MESSAGES, routing=routing, max_tokens=max_tokens
            )
        ]


def last_request_body() -> dict[str, Any]:
    request = respx.calls.last.request
    body: dict[str, Any] = json.loads(request.content)
    return body


# --- happy path --------------------------------------------------------------


@respx.mock
async def test_deltas_arrive_in_order() -> None:
    events = await collect(
        sse_response(
            f"data: {delta_chunk('Hello')}",
            "",
            f"data: {delta_chunk(', ')}",
            "",
            f"data: {delta_chunk('world')}",
            "",
            "data: [DONE]",
        )
    )

    assert events == [TextDelta("Hello"), TextDelta(", "), TextDelta("world")]


@respx.mock
async def test_usage_arrives_last() -> None:
    usage_chunk = json.dumps(
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 30, "cost": 0.000123}}
    )
    events = await collect(
        sse_response(f"data: {delta_chunk('hi')}", f"data: {usage_chunk}", "data: [DONE]")
    )

    assert events[0] == TextDelta("hi")
    assert events[-1] == Usage(prompt_tokens=12, completion_tokens=30, cost=Decimal("0.000123"))


@respx.mock
async def test_cost_does_not_go_through_float() -> None:
    usage_chunk = json.dumps({"usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.1}})
    events = await collect(sse_response(f"data: {usage_chunk}", "data: [DONE]"))

    usage = events[-1]
    assert isinstance(usage, Usage)
    assert usage.cost == Decimal("0.1")


@respx.mock
async def test_usage_without_cost_is_still_reported() -> None:
    usage_chunk = json.dumps({"usage": {"prompt_tokens": 5, "completion_tokens": 7}})
    events = await collect(sse_response(f"data: {usage_chunk}", "data: [DONE]"))

    assert events[-1] == Usage(prompt_tokens=5, completion_tokens=7, cost=None)


# --- stream framing ----------------------------------------------------------


@respx.mock
async def test_keepalive_comments_are_ignored() -> None:
    # OpenRouter sends these while a slow model is still thinking. Parsing one
    # as JSON is the most likely way to break this reader.
    events = await collect(
        sse_response(
            ": OPENROUTER PROCESSING",
            ": OPENROUTER PROCESSING",
            f"data: {delta_chunk('done thinking')}",
            "data: [DONE]",
        )
    )

    assert events == [TextDelta("done thinking")]


@respx.mock
async def test_done_terminates_the_stream() -> None:
    events = await collect(
        sse_response(
            f"data: {delta_chunk('kept')}",
            "data: [DONE]",
            f"data: {delta_chunk('never read')}",
        )
    )

    assert events == [TextDelta("kept")]


@respx.mock
async def test_malformed_chunks_are_skipped_not_fatal() -> None:
    events = await collect(
        sse_response(
            f"data: {delta_chunk('before')}",
            "data: {not valid json",
            f"data: {delta_chunk('after')}",
            "data: [DONE]",
        )
    )

    # One bad fragment should cost that fragment, not the whole answer.
    assert events == [TextDelta("before"), TextDelta("after")]


@respx.mock
async def test_reasoning_is_captured_separately_from_the_answer() -> None:
    # Reasoning models emit these with content empty for as long as they
    # think; dropping them leaves the column blank while the model works.
    reasoning = json.dumps(
        {"choices": [{"delta": {"content": "", "reasoning": "Let me think"}}]},
    )
    events = await collect(
        sse_response(f"data: {reasoning}", f"data: {delta_chunk('4')}", "data: [DONE]")
    )

    assert events == [ReasoningDelta("Let me think"), TextDelta("4")]


@respx.mock
async def test_a_response_that_is_only_reasoning_still_yields_events() -> None:
    # Measured against the real API: a low max_tokens can be spent entirely on
    # reasoning, reporting output tokens while producing no answer at all.
    reasoning = json.dumps({"choices": [{"delta": {"content": "", "reasoning": "thinking"}}]})
    usage = json.dumps({"usage": {"prompt_tokens": 25, "completion_tokens": 24}})
    events = await collect(sse_response(f"data: {reasoning}", f"data: {usage}", "data: [DONE]"))

    assert events == [
        ReasoningDelta("thinking"),
        Usage(prompt_tokens=25, completion_tokens=24, cost=None),
    ]


@respx.mock
async def test_empty_deltas_and_blank_lines_produce_nothing() -> None:
    events = await collect(
        sse_response(
            "",
            f"data: {json.dumps({'choices': [{'delta': {}}]})}",
            f"data: {json.dumps({'choices': [{'delta': {'content': ''}}]})}",
            "data: [DONE]",
        )
    )

    assert events == []


@respx.mock
async def test_a_stream_that_ends_without_done_still_completes() -> None:
    events = await collect(sse_response(f"data: {delta_chunk('truncated')}"))

    assert events == [TextDelta("truncated")]


# --- errors ------------------------------------------------------------------


@respx.mock
async def test_http_error_raises_with_the_model_id() -> None:
    respx.post(COMPLETIONS_URL).mock(
        return_value=httpx.Response(402, json={"error": {"message": "Insufficient credits"}})
    )

    async with httpx.AsyncClient() as http:
        client = OpenRouterClient(make_settings(), http)
        with pytest.raises(CompletionError) as error:
            [event async for event in client.stream_completion(MODEL, MESSAGES)]

    assert error.value.model_id == MODEL
    assert "402" in str(error.value)
    assert "Insufficient credits" in str(error.value)


@respx.mock
async def test_an_error_arriving_mid_stream_raises() -> None:
    respx.post(COMPLETIONS_URL).mock(
        return_value=sse_response(
            f"data: {delta_chunk('starting fine')}",
            f"data: {json.dumps({'error': {'message': 'upstream died', 'code': 502}})}",
            "data: [DONE]",
        )
    )

    collected: list[StreamEvent] = []
    async with httpx.AsyncClient() as http:
        client = OpenRouterClient(make_settings(), http)
        with pytest.raises(CompletionError, match="upstream died"):
            async for event in client.stream_completion(MODEL, MESSAGES):
                collected.append(event)

    # The healthy part of the stream is delivered before the failure.
    assert collected == [TextDelta("starting fine")]


@respx.mock
async def test_a_missing_api_key_fails_clearly() -> None:
    async with httpx.AsyncClient() as http:
        client = OpenRouterClient(make_settings(api_key=None), http)
        with pytest.raises(CompletionError, match="OPENROUTER_API_KEY"):
            [event async for event in client.stream_completion(MODEL, MESSAGES)]


@respx.mock
async def test_transport_failures_become_completion_errors() -> None:
    respx.post(COMPLETIONS_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    async with httpx.AsyncClient() as http:
        client = OpenRouterClient(make_settings(), http)
        with pytest.raises(CompletionError) as error:
            [event async for event in client.stream_completion(MODEL, MESSAGES)]

    assert error.value.model_id == MODEL


# --- request shape -----------------------------------------------------------


@respx.mock
async def test_request_asks_for_streaming_and_usage() -> None:
    await collect(sse_response("data: [DONE]"))
    body = last_request_body()

    assert body["model"] == MODEL
    assert body["stream"] is True
    assert body["usage"] == {"include": True}
    assert body["messages"] == [{"role": "user", "content": "hello"}]


@respx.mock
async def test_default_routing_omits_the_provider_key_entirely() -> None:
    await collect(sse_response("data: [DONE]"), routing=RoutingOptions())

    # Sending an empty object would pin us to today's defaults rather than
    # letting OpenRouter route.
    assert "provider" not in last_request_body()


@respx.mock
async def test_no_routing_omits_the_provider_key() -> None:
    await collect(sse_response("data: [DONE]"), routing=None)

    assert "provider" not in last_request_body()


@respx.mock
async def test_routing_options_are_sent_when_set() -> None:
    await collect(
        sse_response("data: [DONE]"),
        routing=RoutingOptions(
            sort="throughput",
            order=("novita/fp8",),
            allow_fallbacks=False,
            data_collection="deny",
        ),
    )

    assert last_request_body()["provider"] == {
        "sort": "throughput",
        "order": ["novita/fp8"],
        "allow_fallbacks": False,
        "data_collection": "deny",
    }


@respx.mock
async def test_max_tokens_is_omitted_unless_asked_for() -> None:
    await collect(sse_response("data: [DONE]"))
    assert "max_tokens" not in last_request_body()

    await collect(sse_response("data: [DONE]"), max_tokens=256)
    assert last_request_body()["max_tokens"] == 256


@respx.mock
async def test_identifying_headers_are_sent() -> None:
    await collect(sse_response("data: [DONE]"))
    headers = respx.calls.last.request.headers

    assert headers["Authorization"] == "Bearer sk-test-key"
    assert headers["X-OpenRouter-Title"] == "Promptheus"
    assert headers["HTTP-Referer"] == "http://localhost:8000"
