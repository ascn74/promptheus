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
- `scripts/build-css.sh` — new, downloads the pinned CLI and builds
- `.github/workflows/ci.yml` — a check that the built CSS is current
- `README.md`, `plans/done/07-web-ui.md` — record the change

## Scope

### Tailwind without a Node toolchain

Use the **Tailwind standalone CLI**, so a Python project does not grow an npm
install. Tailwind **v4.3.3** configures from CSS — no `tailwind.config.js`, so
the theme lives in `input.css` under `@theme`.

> **Changed during implementation: no `pytailwindcss`.** The plan named it as
> the dev dependency, and it turned out to be the wrong tool for two reasons.
> Its downloader uses `urllib` without a certificate bundle and fails outright
> on a stock macOS python.org install (`CERTIFICATE_VERIFY_FAILED`). More
> importantly it fetches from `releases/latest`, so the build is **not pinned**
> — the very thing this plan warned against two paragraphs later. Replaced by
> `scripts/build-css.sh`, which downloads a pinned binary with `curl` into a
> gitignored `.tailwind/`. That removes a Python dependency rather than adding
> one, and behaves identically on a laptop and on the CI runner.

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

> **Changed during implementation: some visible things land here.** The plan
> kept every visible change for plan 09, which would have committed JavaScript
> nothing could reach — untestable and dead until a later PR. So the `copy` and
> `collapse` links and the progress line ship with the code that drives them.
>
> Column wrapping also moved here: porting the grid meant deciding what to do
> with `--column-count`, and faithfully carrying over a layout everyone had
> already agreed to replace made no sense. Plan 09 drops both bullets.
>
> One genuine regression was caught and fixed while checking the port:
> Tailwind's preflight strips the native file-input button, which left
> *Attachments* as bare text. It is restored with `file:` utilities.

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

**The binary is downloaded on first use.** That is a network dependency at
build time, including in CI, but only for *building* the CSS — running the app
needs nothing, because the output is committed. The version is pinned in
`scripts/build-css.sh`; bumping it is a deliberate edit with a visible diff.

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
