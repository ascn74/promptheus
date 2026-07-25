"""Tests for the fan-out orchestrator.

Ordering is made deterministic with asyncio primitives rather than sleeps, so
the suite does not depend on how loaded the machine is.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing

import pytest

from promptheus.attachments import Attachment
from promptheus.openrouter import (
    CompletionError,
    Message,
    ReasoningDelta,
    RoutingOptions,
    StreamEvent,
    TextDelta,
    Usage,
)
from promptheus.orchestrator import Event, Run, RunRegistry, execute


class FakeClient:
    """A completion client driven entirely by the test.

    Each model gets a list of steps: a `StreamEvent` to emit, an exception to
    raise, or an `asyncio.Event` to wait on before continuing.
    """

    def __init__(self, scripts: dict[str, list[object]]) -> None:
        self.scripts = scripts
        self.started: list[str] = []
        self.cancelled: list[str] = []
        self.finished: list[str] = []
        self.calls: list[tuple[str, Sequence[Message], RoutingOptions | None, int | None]] = []

    async def stream_completion(
        self,
        model_id: str,
        messages: Sequence[Message],
        routing: RoutingOptions | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.started.append(model_id)
        self.calls.append((model_id, messages, routing, max_tokens))
        try:
            for step in self.scripts.get(model_id, []):
                if isinstance(step, asyncio.Event):
                    await step.wait()
                elif isinstance(step, Exception):
                    raise step
                else:
                    assert isinstance(step, TextDelta | ReasoningDelta | Usage)
                    yield step
            self.finished.append(model_id)
        except asyncio.CancelledError:
            # Recorded so a test can prove the task was really cancelled and
            # not merely dropped on the floor.
            self.cancelled.append(model_id)
            raise


def make_run(*model_ids: str, prompt: str = "hello", **kwargs: object) -> Run:
    registry = RunRegistry(ttl_seconds=3600.0)
    return registry.create(prompt, [], list(model_ids), **kwargs)  # type: ignore[arg-type]


async def drain(run: Run, client: FakeClient) -> list[Event]:
    async with aclosing(execute(run, client)) as stream:
        return [event async for event in stream]


# --- fan-out -----------------------------------------------------------------


async def test_every_model_runs() -> None:
    client = FakeClient(
        {
            "a/one": [TextDelta("A")],
            "b/two": [TextDelta("B")],
            "c/three": [TextDelta("C")],
        }
    )
    run = make_run("a/one", "b/two", "c/three")

    events = await drain(run, client)

    assert sorted(client.started) == ["a/one", "b/two", "c/three"]
    assert {event.slug for event in events} == {"m0", "m1", "m2"}


async def test_events_carry_the_slug_of_their_column() -> None:
    client = FakeClient({"a/one": [TextDelta("first")], "b/two": [TextDelta("second")]})
    run = make_run("a/one", "b/two")

    events = await drain(run, client)

    by_slug = {event.slug: event.text for event in events if event.type == "delta"}
    assert by_slug == {"m0": "first", "m1": "second"}


async def test_output_interleaves_rather_than_running_in_series() -> None:
    slow_gate = asyncio.Event()
    client = FakeClient(
        {
            "slow/model": [TextDelta("slow-start"), slow_gate, TextDelta("slow-end")],
            "fast/model": [TextDelta("fast-1"), TextDelta("fast-2")],
        }
    )
    run = make_run("slow/model", "fast/model")

    collected: list[Event] = []
    async with aclosing(execute(run, client)) as stream:
        async for event in stream:
            collected.append(event)
            # The fast model must be able to finish while the slow one is
            # still blocked; releasing only after proves they overlap.
            if event.slug == "m1" and event.type == "done":
                slow_gate.set()

    texts = [event.text for event in collected if event.type == "delta"]
    assert texts.index("fast-2") < texts.index("slow-end")


async def test_reasoning_and_text_are_distinct_event_types() -> None:
    client = FakeClient({"a/one": [ReasoningDelta("thinking"), TextDelta("answer")]})
    run = make_run("a/one")

    events = await drain(run, client)

    assert [(event.type, event.text) for event in events[:2]] == [
        ("reasoning", "thinking"),
        ("delta", "answer"),
    ]


# --- termination -------------------------------------------------------------


async def test_stream_ends_only_after_every_column_finishes() -> None:
    gate = asyncio.Event()
    client = FakeClient(
        {
            "fast/model": [TextDelta("done early")],
            "slow/model": [gate, TextDelta("finally")],
        }
    )
    run = make_run("fast/model", "slow/model")

    collected: list[Event] = []
    async with aclosing(execute(run, client)) as stream:
        async for event in stream:
            collected.append(event)
            if len(collected) == 2:
                # Both fast events are in; the stream must still be open.
                gate.set()

    terminal = [event for event in collected if event.type in ("done", "error")]
    assert {event.slug for event in terminal} == {"m0", "m1"}


async def test_each_column_reports_exactly_one_terminal_event() -> None:
    client = FakeClient({"a/one": [TextDelta("x")], "b/two": [CompletionError("b/two", "nope")]})
    run = make_run("a/one", "b/two")

    events = await drain(run, client)

    terminal = [event for event in events if event.type in ("done", "error")]
    assert len(terminal) == 2


async def test_usage_rides_along_with_the_done_event() -> None:
    usage = Usage(prompt_tokens=10, completion_tokens=20)
    client = FakeClient({"a/one": [TextDelta("x"), usage]})
    run = make_run("a/one")

    events = await drain(run, client)

    assert events[-1].type == "done"
    assert events[-1].usage == usage


async def test_a_run_with_no_models_ends_immediately() -> None:
    assert await drain(make_run(), FakeClient({})) == []


# --- failure isolation -------------------------------------------------------


async def test_one_failing_model_does_not_take_down_the_run() -> None:
    client = FakeClient(
        {
            "good/one": [TextDelta("fine")],
            "bad/model": [CompletionError("bad/model", "upstream died")],
            "good/two": [TextDelta("also fine")],
        }
    )
    run = make_run("good/one", "bad/model", "good/two")

    events = await drain(run, client)

    errors = [event for event in events if event.type == "error"]
    assert len(errors) == 1
    assert errors[0].slug == "m1"
    assert "upstream died" in errors[0].text
    # Eight good answers and one visible failure is the correct outcome.
    assert {event.slug for event in events if event.type == "done"} == {"m0", "m2"}


async def test_an_unexpected_exception_also_becomes_an_error_event() -> None:
    client = FakeClient({"a/one": [RuntimeError("something odd")]})
    run = make_run("a/one")

    events = await drain(run, client)

    assert events[-1].type == "error"
    assert "something odd" in events[-1].text


# --- cancellation ------------------------------------------------------------


async def test_abandoning_the_stream_cancels_the_pending_requests() -> None:
    never = asyncio.Event()
    client = FakeClient(
        {
            "a/one": [TextDelta("first"), never],
            "b/two": [never],
            "c/three": [never],
        }
    )
    run = make_run("a/one", "b/two", "c/three")

    stream = execute(run, client)
    first = await anext(stream)
    assert first.type == "delta"

    await stream.aclose()

    # Closing the tab must not leave three requests running and billing.
    assert sorted(client.cancelled) == ["a/one", "b/two", "c/three"]
    assert client.finished == []


async def test_tasks_do_not_outlive_a_completed_run() -> None:
    client = FakeClient({"a/one": [TextDelta("x")]})
    run = make_run("a/one")

    before = len(asyncio.all_tasks())
    await drain(run, client)

    assert len(asyncio.all_tasks()) == before


# --- request wiring ----------------------------------------------------------


async def test_the_composed_prompt_reaches_every_model() -> None:
    client = FakeClient({"a/one": [], "b/two": []})
    registry = RunRegistry(ttl_seconds=3600.0)
    run = registry.create(
        "Summarise",
        [Attachment(filename="notes.txt", text="body text", kind="text")],
        ["a/one", "b/two"],
    )

    await drain(run, client)

    for _model_id, messages, _routing, _max_tokens in client.calls:
        content = messages[0]["content"]
        assert "Summarise" in content
        assert '<attachment name="notes.txt">' in content
        assert "body text" in content


async def test_routing_and_token_cap_are_passed_through() -> None:
    client = FakeClient({"a/one": []})
    routing = RoutingOptions(sort="price")
    run = make_run("a/one", max_output_tokens=256, routing=routing)

    await drain(run, client)

    _model_id, _messages, sent_routing, max_tokens = client.calls[0]
    assert sent_routing is routing
    assert max_tokens == 256


# --- registry ----------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_registry_hands_back_the_run_it_created() -> None:
    registry = RunRegistry(ttl_seconds=3600.0)
    run = registry.create("prompt", [], ["a/one"])

    assert registry.get(run.id) is run


def test_registry_returns_none_for_an_unknown_id() -> None:
    assert RunRegistry(ttl_seconds=3600.0).get("nope") is None


def test_run_ids_are_unguessable_and_unique() -> None:
    registry = RunRegistry(ttl_seconds=3600.0)
    ids = {registry.create("p", [], ["a/one"]).id for _ in range(50)}

    assert len(ids) == 50
    assert all(len(run_id) >= 12 for run_id in ids)


def test_runs_are_evicted_once_the_ttl_passes() -> None:
    clock = FakeClock()
    registry = RunRegistry(ttl_seconds=600.0, clock=clock)
    run = registry.create("prompt", [], ["a/one"])

    clock.advance(599.0)
    assert registry.get(run.id) is not None

    clock.advance(2.0)
    assert registry.get(run.id) is None
    assert len(registry) == 0


def test_eviction_spares_runs_still_inside_the_ttl() -> None:
    clock = FakeClock()
    registry = RunRegistry(ttl_seconds=600.0, clock=clock)
    old = registry.create("old", [], ["a/one"])
    clock.advance(500.0)
    fresh = registry.create("fresh", [], ["b/two"])

    clock.advance(200.0)

    assert registry.get(old.id) is None
    assert registry.get(fresh.id) is not None


def test_columns_are_slugged_in_order() -> None:
    run = make_run("a/one", "b/two", "c/three")

    assert [column.slug for column in run.columns] == ["m0", "m1", "m2"]
    assert run.column_for("m1") is not None
    assert run.column_for("m1").model_id == "b/two"  # type: ignore[union-attr]
    assert run.column_for("nope") is None


@pytest.mark.parametrize("model_id", ["anthropic/claude-opus-5", "google/gemini-3.1-pro-preview"])
def test_slugs_never_contain_characters_that_break_sse_or_html(model_id: str) -> None:
    run = make_run(model_id)

    # Model ids carry `/` and `.`; event names and HTML attributes must not.
    assert run.columns[0].slug.isalnum()
