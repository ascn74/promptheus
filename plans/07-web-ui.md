# 07 — Web interface

The FastAPI app, the Jinja templates and the htmx wiring.

**Depends on:** 02, 04, 06

## Files

- `src/promptheus/app.py`
- `src/promptheus/routes.py`
- `src/promptheus/templates/` — `base.html`, `index.html`, `_model_list.html`,
  `_columns.html`, `_estimate.html`
- `src/promptheus/static/` — vendored `htmx.min.js`, the SSE extension, `app.css`
- `tests/test_routes.py`

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | the composer: prompt, files, presets, model list |
| `GET` | `/models` | filtered model list fragment (htmx) |
| `POST` | `/estimate` | cost fragment for the current selection (htmx) |
| `GET` | `/models/{id}/endpoints` | provider override fragment, lazy |
| `POST` | `/runs` | accepts prompt + files, returns the empty columns |
| `GET` | `/runs/{id}/stream` | the multiplexed SSE stream |

## Design notes

**Search is server-side.** `GET /models` takes a query and filter parameters
and returns an HTML fragment. The template drives it with:

```html
<input name="q" hx-get="/models" hx-trigger="keyup changed delay:200ms"
       hx-target="#model-list">
```

No client-side state, no model list shipped to the browser.

**One SSE connection, one event per column.** The columns container connects
once and each column subscribes to its own event name:

```html
<div hx-ext="sse" sse-connect="/runs/{{ run.id }}/stream">
  <div sse-swap="m0" hx-swap="beforeend"></div>
  <div sse-swap="m1" hx-swap="beforeend"></div>
</div>
```

htmx opens a single `EventSource` and routes each named event to the matching
element. This is what keeps us under the browser's ~6-connections-per-origin
cap on HTTP/1.1 — one stream per model would stall the seventh column with no
error message anywhere.

**Markdown lands at the end.** Deltas append as escaped plain text while the
answer streams; on `done`, emit a final event replacing the column with
markdown rendered server-side by `markdown-it-py`. Rendering markdown on every
delta means re-parsing unclosed code fences dozens of times per second, for a
worse result.

**Escape every delta.** Model output is untrusted and goes straight into the
DOM. Escape it on the server as it is emitted.

**Estimate before running.** `POST /estimate` recomputes on every change to the
prompt, the attachments or the selection, showing per-model and total figures,
labelled as an estimate, with context overflows and attachment warnings called
out. The run button stays available — the point is to inform, not to block.

**Provider override, lazily.** The control only renders for models with more
than one endpoint, fetched on expand. Around half the catalog has a single
provider and shows nothing at all.

**Vendor the JavaScript.** Commit `htmx.min.js` rather than pointing at a CDN:
this is a local tool that should work offline, and it pins the version.

## Acceptance criteria

- `GET /` renders with presets and the default model list.
- `GET /models?q=claude` returns a fragment containing only matching models.
- `POST /runs` with a prompt and a file creates a run and returns one column
  per selected model, each with the right `sse-swap` name.
- `GET /runs/{id}/stream` emits `text/event-stream` with correctly named
  events, and closes when the run completes.
- An unknown run id returns 404.
- A model producing `<script>` in its output shows it as text; it does not
  execute.
- No route ever puts the API key in a response.

## Tests

`TestClient` against a fake orchestrator. Assert on the SSE body text
(event names and ordering) rather than trying to drive htmx — the wiring is
declarative and verified by reading the emitted HTML.

## Out of scope

Authentication (this is a local tool), saved history, and diffing answers.
