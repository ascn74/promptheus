# 10 — Silence the starlette.testclient deprecation

Adopt `httpx2` for the test client, and make the suite fail on the next
deprecation instead of accumulating it.

**Depends on:** nothing

## The warning

Every run ends with:

```
StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is
deprecated; install `httpx2` instead.
```

`starlette/testclient.py` tries `import httpx2 as httpx`, falls back to `httpx`,
and warns. Nothing is broken today; the fallback works. But the fallback is
what a deprecation removes, so this is a bill that arrives later.

## What the investigation changed

The obvious reading — "migrate the project to httpx2" — is the wrong one.

**`respx` has no `httpx2` support.** It declares `httpx>=0.25.0`, and the suite
uses it **62 times** across four test modules. It is how every test avoids the
network. Moving the application to `httpx2` would mean replacing the entire
mocking layer, which is wildly out of proportion to a warning line.

**`httpx2` and `httpx` coexist.** They are separate distributions with separate
import names (`httpx2`, `httpcore2`), so installing one does not disturb the
other. A dry run adds `httpx2`, `httpcore2` and `truststore` and touches
nothing already installed.

**The two uses are unrelated.** `TestClient` uses its HTTP client to drive
requests *into* the ASGI app. `respx` intercepts the requests our code makes
*out* to OpenRouter. They never meet.

So: install `httpx2` for the test client, leave the application and `respx` on
`httpx`. Ten lines of application code touch `httpx`
(`app.py`, `catalog.py`, `openrouter.py`) and none of them change.

## Files

- `pyproject.toml` — `httpx2` as a dev dependency; pytest `filterwarnings`
- `plans/TODO.md` — drop the entry once this lands

## Scope

1. Add `httpx2>=2.9` to `[project.optional-dependencies].dev`. It is **not** a
   runtime dependency: nothing outside the test suite imports it.
2. Confirm `TestClient` picks it up and all 169 tests still pass. The suite
   asserts on response bodies and SSE framing, so a client swap that changed
   behaviour would show up immediately.
3. Turn warnings into errors in `[tool.pytest.ini_options]`:

   ```toml
   filterwarnings = ["error"]
   ```

   with an explicit `ignore` entry for anything third-party and unavoidable,
   each carrying a comment saying why.

## Design notes

**Step 3 is the point.** Removing one warning is worth little; a suite that
cannot accumulate the next one is worth a lot. This warning went unremarked
through six plans precisely because a single line of yellow text at the end of
a green run is easy to stop seeing.

**Two HTTP stacks in the dev environment is a real cost**, and worth naming
rather than hiding. It is the cheaper of the two options today only because
`respx` has not moved. Revisit if either of these changes:

- `respx` gains `httpx2` support — then migrate the application too and drop
  `httpx`
- `starlette` removes the `httpx` fallback — then the migration stops being
  optional

**Do not add `httpx2` to runtime dependencies.** The application's own client
is `httpx` and stays that way; shipping a second HTTP library to users who
never import it would be pure weight.

## Acceptance criteria

- `pytest` runs with **zero** warnings.
- All 169 tests pass unchanged.
- `filterwarnings = ["error"]` is in effect, and any ignore is justified in a
  comment.
- `httpx2` appears only under dev dependencies.
- The application still imports `httpx`, and `respx` still intercepts it.

## Tests

No new tests. The existing suite is the check: if swapping the client under
`TestClient` changed any behaviour, the assertions on rendered markup and SSE
event framing would catch it.

## Out of scope

Migrating the application to `httpx2`, and replacing `respx`. See the design
notes for what would have to change first.
