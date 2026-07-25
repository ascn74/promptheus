# 05 — OpenRouter streaming client

Stream a chat completion from a single model.

**Depends on:** 01

## Files

- `src/promptheus/openrouter.py`
- `tests/test_openrouter.py`

## Scope

```python
@dataclass(frozen=True)
class RoutingOptions:
    sort: Literal["price", "throughput", "latency"] | None = None
    order: list[str] | None = None      # provider slugs, e.g. "novita/fp8"
    allow_fallbacks: bool = True
    data_collection: Literal["allow", "deny"] = "allow"
```

- `stream_completion(model_id, messages, routing, max_tokens)` — an async
  generator yielding `TextDelta`, then a final `Usage` carrying real token
  counts and cost when OpenRouter reports them.
- Errors surface as a `CompletionError` with the model id attached, never as a
  bare exception — the caller is running nine of these and needs to know which
  one failed.

## Design notes

**Request shape.** `POST /chat/completions` with `stream: true`, the
`Authorization` bearer, and the `HTTP-Referer` / `X-OpenRouter-Title` headers
from settings. Routing goes in a `provider` object, omitted entirely when no
options are set so OpenRouter applies its own defaults:

```json
{"provider": {"sort": "price", "order": ["novita/fp8"], "allow_fallbacks": false}}
```

**Parsing SSE from OpenRouter.** Lines are `data: {...}`, terminated by
`data: [DONE]`. OpenRouter also emits `: OPENROUTER PROCESSING` comment lines
as keep-alives during long waits — these must be skipped, not parsed as JSON.
This is the single most likely thing to break here.

**Ask for usage.** Send `"usage": {"include": true}` so the final chunk carries
real prompt/completion token counts and cost. That turns the plan-04 estimate
into an actual figure once the run is done.

**Errors mid-stream.** A stream can start fine and then deliver an error
object instead of a delta. Handle a chunk carrying `error` at any point, not
only at HTTP-status time.

**Timeouts.** Use `request_timeout_seconds` from settings, and no read timeout
tight enough to kill a slow reasoning model mid-thought.

**No retries here.** A partially streamed answer cannot be retried without
showing the user duplicated text. Fail the column and let the user re-run.

## Acceptance criteria

- A fake SSE stream of several deltas yields them in order, ending with usage.
- `: OPENROUTER PROCESSING` keep-alive lines are ignored.
- `data: [DONE]` terminates the generator cleanly.
- An HTTP 4xx/5xx and an in-stream error object both raise `CompletionError`
  carrying the model id.
- `RoutingOptions()` with no fields set produces a body with **no** `provider`
  key at all.

## Tests

`respx` with hand-written SSE bodies, including a malformed chunk and a
keep-alive line. No real network calls.

## Out of scope

Concurrency across models (plan 06).
