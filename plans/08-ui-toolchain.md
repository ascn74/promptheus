# 08 — UI toolchain: Tailwind and first-party JavaScript

Replace the hand-written stylesheet with Tailwind, and add a small JavaScript
file. **No visible change** beyond what falls out of a faithful port.

**Depends on:** 07

## Why this reverses plan 07

Plan 07 chose hand-written CSS and "no custom JavaScript", on the grounds that
htmx could carry the whole interface. That held for the plumbing, but the
improvements in plan 09 need behaviour htmx cannot express — copying an
answer, collapsing a column, counting completed columns — and the stylesheet
has already reached the size where consistency is maintained by hand.

This is a deliberate reversal, recorded here and noted in
[`done/07-web-ui.md`](done/07-web-ui.md).

It lands **before** plan 09 and on its own: a Tailwind migration rewrites every
`class` attribute in every template. Mixed with UX changes, neither diff would
be reviewable.

## Files

- `src/promptheus/static/input.css` — new, the Tailwind entry point
- `src/promptheus/static/app.css` — becomes build output, still committed
- `src/promptheus/static/app.js` — new
- `src/promptheus/templates/*.html` — class attributes ported
- `pyproject.toml` — `pytailwindcss` as a dev dependency
- `.github/workflows/ci.yml` — a check that the built CSS is current
- `README.md`, `plans/done/07-web-ui.md` — record the change

## Scope

### Tailwind without a Node toolchain

Use the **Tailwind standalone CLI**, installed through `pytailwindcss` (0.3.1,
`requires-python >=3.11`) so a Python project does not grow an npm install.
Tailwind is at **v4.3.3**, which configures from CSS — no `tailwind.config.js`,
so the theme lives in `input.css` under `@theme`.

Port the tokens currently in `:root` and the `prefers-color-scheme` block
(`static/app.css:1-25`) into that `@theme`. The palette stays as it is; this
plan changes how styles are produced, not how they look.

Build:

```bash
tailwindcss -i src/promptheus/static/input.css -o src/promptheus/static/app.css --minify
```

The output is **committed**, exactly as htmx was vendored: the runtime stays
dependency-free and works offline, and nobody needs a toolchain to run the app.

### First-party JavaScript

`static/app.js`, loaded from `base.html` after htmx. No framework, no build
step, target 40–60 lines:

- copy a column's answer to the clipboard
- collapse and expand a column
- aggregate progress, by listening for `htmx:sseMessage` on `.results` and
  counting events whose name ends in `-done`

Plan 09 consumes these; this plan only has to land them working.

## Design notes

**CI must verify the committed CSS is current.** Rebuild in the workflow and
fail if the result differs from what is committed:

```bash
tailwindcss -i src/promptheus/static/input.css -o /tmp/app.css --minify
diff -q /tmp/app.css src/promptheus/static/app.css
```

Without this, a template gains a class, nobody rebuilds, and the next person to
pull ships a page with missing styles — a failure that is invisible until
someone looks at the screen.

**`pytailwindcss` downloads the binary on first use.** That is a network
dependency at build time, including in CI. Acceptable, but pin the Tailwind
version rather than tracking latest, and if CI flakiness appears, download the
`tailwindcss-linux-x64` asset directly in the workflow instead.

**The port must not change behaviour.** `tests/test_routes.py` asserts on
rendered markup — `sse-swap` names, hidden inputs, escaped output. Those
assertions are the guardrail; if one breaks during the port, the port is wrong,
not the test.

**Keep the semantic hooks.** Class names used as selectors by JavaScript,
tests or the SSE wiring (`.results`, `.column`, `.answer`, `.reasoning-body`)
stay as they are. Tailwind utilities go alongside them, not instead of them.

## Acceptance criteria

- The page is visually unchanged in both colour schemes.
- `app.css` is a build artefact of `input.css`, committed, and CI fails if the
  two disagree.
- The app still runs with no network access and no toolchain installed.
- `app.js` provides copy, collapse and a completed-column count.
- All 156 existing tests pass untouched.

## Tests

No new tests here — this plan is a port. The existing markup assertions in
`tests/test_routes.py` are what proves the port was faithful.

## Out of scope

Every visible change: those are plan 09.
