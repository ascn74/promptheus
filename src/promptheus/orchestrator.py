"""Run N models concurrently and merge their output into one event stream."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal, Protocol

from promptheus.attachments import Attachment
from promptheus.estimate import compose_message
from promptheus.openrouter import (
    CompletionError,
    Message,
    ReasoningDelta,
    RoutingOptions,
    StreamEvent,
    TextDelta,
    Usage,
)

logger = logging.getLogger(__name__)

EventType = Literal["delta", "reasoning", "done", "error"]

_TERMINAL: frozenset[EventType] = frozenset({"done", "error"})


class CompletionStreamer(Protocol):
    """What the orchestrator needs from a completion client."""

    def stream_completion(
        self,
        model_id: str,
        messages: Sequence[Message],
        routing: RoutingOptions | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]: ...


@dataclass(frozen=True)
class Column:
    """One model's place in the comparison.

    The slug — `m0`, `m1`, … — is what names the SSE event and the target
    element. Model ids contain `/` and `.`, which are asking for trouble in
    event names and HTML attributes, so they never appear there.
    """

    slug: str
    model_id: str


@dataclass(frozen=True)
class Event:
    slug: str
    type: EventType
    text: str = ""
    usage: Usage | None = None


@dataclass(frozen=True)
class Run:
    id: str
    columns: tuple[Column, ...]
    prompt: str
    attachments: tuple[Attachment, ...]
    created_at: float
    max_output_tokens: int | None = None
    routing: RoutingOptions | None = None

    @property
    def message_text(self) -> str:
        """The exact text sent to every model — the same one plan 04 priced."""
        return compose_message(self.prompt, self.attachments)

    @property
    def messages(self) -> list[Message]:
        return [{"role": "user", "content": self.message_text}]

    @property
    def model_ids(self) -> tuple[str, ...]:
        return tuple(column.model_id for column in self.columns)

    def column_for(self, slug: str) -> Column | None:
        return next((column for column in self.columns if column.slug == slug), None)


@dataclass
class RunRegistry:
    """Pending runs, held in memory between the upload and the stream.

    SSE is a GET, so the files cannot be uploaded on the same request that
    streams the answers; the run has to survive between the two. This is
    process memory with a deadline, not storage — it dies with the process,
    which is exactly what "no persistence" was meant to mean.
    """

    ttl_seconds: float
    clock: Callable[[], float] = monotonic
    _runs: dict[str, Run] = field(default_factory=dict)

    def create(
        self,
        prompt: str,
        attachments: Sequence[Attachment],
        model_ids: Sequence[str],
        max_output_tokens: int | None = None,
        routing: RoutingOptions | None = None,
    ) -> Run:
        self.evict_expired()
        run = Run(
            id=secrets.token_urlsafe(12),
            columns=tuple(
                Column(slug=f"m{index}", model_id=model_id)
                for index, model_id in enumerate(model_ids)
            ),
            prompt=prompt,
            attachments=tuple(attachments),
            created_at=self.clock(),
            max_output_tokens=max_output_tokens,
            routing=routing,
        )
        self._runs[run.id] = run
        return run

    def get(self, run_id: str) -> Run | None:
        self.evict_expired()
        return self._runs.get(run_id)

    def discard(self, run_id: str) -> None:
        self._runs.pop(run_id, None)

    def evict_expired(self) -> None:
        deadline = self.clock() - self.ttl_seconds
        for run_id in [key for key, run in self._runs.items() if run.created_at < deadline]:
            del self._runs[run_id]

    def __len__(self) -> int:
        return len(self._runs)


async def execute(run: Run, client: CompletionStreamer) -> AsyncGenerator[Event, None]:
    """Stream every column's output, interleaved, as it arrives.

    One task per model, all pushing onto a single queue. The stream ends once
    every column has reported a terminal event — counted here rather than
    awaited on the tasks, so the last column finishing ends the stream at once.

    Returns an async *generator*, not merely an iterator: callers that stop
    early must call `aclose()` (directly or via `contextlib.aclosing`) to get
    the cancellation guarantee below. Typing this as `AsyncIterator` would hide
    the one method that makes abandoning the stream safe.
    """
    queue: asyncio.Queue[Event] = asyncio.Queue()
    tasks = [
        asyncio.create_task(_run_column(column, run, client, queue), name=f"run-{column.slug}")
        for column in run.columns
    ]

    remaining = len(run.columns)
    try:
        while remaining:
            event = await queue.get()
            if event.type in _TERMINAL:
                remaining -= 1
            yield event
    finally:
        # Reached on normal completion and, crucially, when the consumer walks
        # away: closing the browser tab must not leave N requests running and
        # billing in the background.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _run_column(
    column: Column,
    run: Run,
    client: CompletionStreamer,
    queue: asyncio.Queue[Event],
) -> None:
    usage: Usage | None = None
    try:
        async for event in client.stream_completion(
            column.model_id,
            run.messages,
            routing=run.routing,
            max_tokens=run.max_output_tokens,
        ):
            match event:
                case TextDelta(text=text):
                    await queue.put(Event(column.slug, "delta", text=text))
                case ReasoningDelta(text=text):
                    await queue.put(Event(column.slug, "reasoning", text=text))
                case Usage():
                    usage = event
        await queue.put(Event(column.slug, "done", usage=usage))
    except CompletionError as error:
        # One model failing is a failed column, not a failed run. Eight good
        # answers and one visible error is the correct outcome.
        await queue.put(Event(column.slug, "error", text=error.message))
    except Exception as error:
        # CancelledError is a BaseException, so it passes through untouched and
        # cancellation stays prompt.
        logger.exception("unexpected failure streaming %s", column.model_id)
        await queue.put(Event(column.slug, "error", text=str(error) or type(error).__name__))
