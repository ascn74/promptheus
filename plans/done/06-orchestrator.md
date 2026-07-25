# 06 — Fan-out orchestrator

Run N models concurrently and merge their output into one event stream.

**Depends on:** 05

## Files

- `src/promptheus/orchestrator.py`
- `tests/test_orchestrator.py`

## Scope

```python
@dataclass(frozen=True)
class Column:
    slug: str          # "m0", "m1", ... — the SSE event name
    model_id: str

@dataclass(frozen=True)
class Event:
    slug: str
    type: Literal["delta", "done", "error"]
    text: str = ""
    usage: Usage | None = None
```

- `Run` — holds the columns, the prompt, the attachments and a creation
  timestamp.
- `RunRegistry` — an in-memory `dict[str, Run]` with TTL eviction.
- `execute(run) -> AsyncIterator[Event]` — starts one task per model and yields
  events from all of them as they arrive.

## Design notes

**One queue, many producers.** Each model gets a task that consumes
`stream_completion` and pushes events onto a shared `asyncio.Queue`. The
generator drains that queue until every column has reported `done` or `error`.
Count terminal events rather than waiting on the tasks, so the stream ends the
moment the last column finishes.

**Short opaque slugs, not model ids.** The slug becomes an SSE event name, and
model ids contain `/` and `.` — characters that are asking for trouble in
event names and in HTML attributes. `m0`, `m1`, … sidestep it. The mapping
lives in the `Run`.

**A failing model must not take down the run.** Each task wraps its work so a
`CompletionError` becomes an `error` event for that column only. Eight good
answers plus one visible failure is the correct outcome, not a 500.

**Cancellation.** When the client disconnects, the generator is closed; cancel
every outstanding task in a `finally` block. Without this, closing the browser
tab leaves nine requests running and billing.

**The registry is why "no persistence" still holds.** SSE is a GET, so the
upload and the stream cannot be the same request; the run has to survive
between the two. This is process memory with a TTL, not storage — it dies with
the process, which is exactly what was intended.

**`execute` returns an async generator, not merely an iterator.** The
cancellation guarantee above depends on the caller invoking `aclose()`, whether
directly or through `contextlib.aclosing`. Typing the return as
`AsyncIterator` would hide the one method that makes abandoning the stream
safe.

**Reasoning gets its own event type.** Plan 05 found that models emit
`delta.reasoning` while `content` is still empty; `Event.type` carries
`reasoning` alongside `delta` so the interface can show progress separately
from the answer being compared.

## Acceptance criteria

- Three fake models streaming at different speeds produce interleaved events,
  each tagged with the right slug.
- The stream ends only after all three reach a terminal event.
- One model raising yields an `error` event for that column while the other two
  complete normally.
- Abandoning the generator early cancels the pending tasks — assert they are
  actually cancelled, not merely abandoned.
- Runs older than the TTL are evicted from the registry.

## Tests

Fake `stream_completion` with async generators and controlled delays. Use
`asyncio` primitives to make ordering deterministic rather than sleeping and
hoping.

## Out of scope

HTTP endpoints and rendering (plan 07).
