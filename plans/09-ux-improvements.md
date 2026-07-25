# 09 — UX improvements

Make the interface usable rather than merely working.

**Depends on:** 08

## The diagnosis

The interface was built to prove the plumbing. Driving it in a browser surfaced
one complaint — the preset list is easy to confuse with the model list — which
turns out to be a symptom of something structural.

### 1. Selecting and browsing share one region and one visual language

The Models panel (`templates/index.html:44-100`) stacks four controls that do
two fundamentally different things:

| Control | Verb |
|---|---|
| Preset chips | **selects** models |
| Search box | **filters** what is shown |
| "Free only" checkbox | **filters** what is shown |
| Row checkboxes | **selects** models |

They are interleaved, and both kinds are dressed as pills and checkboxes.
Nothing says that clicking *Anthropic — Claude* changes what will run, while
typing *claude* changes only what is visible.

Three details sharpen it:

- **Presets have no state.** Nothing marks one as active, so clicking gives no
  confirmation that anything happened.
- **Presets accumulate.** They union with the current selection
  (`routes.py:169-176`) — reasonable, but invisible and therefore surprising.
- **"Clear" wears the same clothes.** It sits in the chip row, styled
  identically, and is the only destructive control there.

### 2. The selection is invisible

The most important state — *which models will run* — has no display. The list
holds 337 rows in a 340px scroll box, so after choosing a preset the user may
see none of the nine checked boxes. The only feedback is the estimate table
further down, which is a cost breakdown that happens to list names.

### 3. Prices are unreadable

Rows show `in $0.000005/tok` (`_model_list.html:23`). Nobody compares
`$0.000005` against `$0.0000009` at a glance — and the row shows **input price
only**, while plan 06 measured the output ceiling at roughly **600x** the input
cost. The one number shown is the one that matters least.

### 4. The primary action is below the fold

`Run comparison` sits under three tall panels. On a laptop the user scrolls
past the whole composer to reach it, leaving the cost they are committing to
somewhere above.

### 5. Results are hard to reach and hard to compare

`#results` is below the entire form and nothing scrolls it into view. Columns
are laid out as `repeat(var(--column-count), minmax(320px, 1fr))`, so nine
models is ~2900px — comparison by horizontal scrolling. There is no aggregate
progress, no copy, and no way to collapse a column already read.

## Files

- `src/promptheus/templates/index.html`, `_model_list.html`, `_estimate.html`,
  `_columns.html`, `_column_final.html`
- `src/promptheus/routes.py` — `model_list` gains `remove` and a total count
- `src/promptheus/static/app.js`, `input.css` — from plan 08
- `tests/test_routes.py`

## Scope

### 9.1 Split selecting from browsing — *fixes 1 and 2*

Restructure the Models panel into two clearly separate zones.

**A "Selected" tray at the top.** One chip per selected model, each with an
`×`, plus a count and a single `Clear all`. This is the fix for the reported
confusion: presets visibly *fill the tray*, and search visibly does not touch
it.

Removal needs no JavaScript. Add a `remove` query parameter to the existing
`model_list` endpoint (`routes.py:160-198`):

```html
<button hx-get="/models?remove={{ model.id }}"
        hx-include="#composer" hx-target="#model-list">×</button>
```

The route drops the id from `selected` before rendering, and the existing
`htmx:afterSwap from:#model-list` trigger (`index.html:113-116`) already
refreshes the estimate.

**A "Browse" zone below**, visually subordinate, holding search, filters and
the list. Move `Clear all` out of the chip row into the tray, beside the
selection it clears.

Label both groups in words — "Presets add to your selection", "Search filters
the list below" — so the verbs are stated rather than inferred.

### 9.2 Make prices legible — *fixes 3*

Show **per-million-token** prices, the industry convention, and show **both**
sides: `$5.00/M in · $25.00/M out`.

No new code: reuse the `usd` filter registered at `routes.py:33`
(`format_usd` in `estimate.py`).

```jinja
{{ (model.pricing.prompt * 1000000) | usd }}/M in
```

Keep the existing `free` and `variable price` branches
(`_model_list.html:21-24`) — the `-1` routers must not render as a price.

### 9.3 Sticky action bar — *fixes 4*

Pin a bar to the bottom of the viewport carrying the summary and the primary
action: `9 models · input $0.0007 · output up to $0.41` and `Run comparison`.

Keep the button itself in `index.html`, so its identity stays stable for
`hx-disabled-elt`, and have `_estimate.html` update the summary through an
htmx **out-of-band swap** into a `#runbar-summary` element. One server render
feeds two places, with no JavaScript.

The Estimate panel stays for the per-model breakdown; the bar is the summary.

### 9.4 Results that can be read — *fixes 5*

- **Wrap the columns**: `repeat(auto-fit, minmax(340px, 1fr))`, so two to four
  sit side by side and the rest wrap onto the next row. Drop the inline
  `--column-count` from `_columns.html:11`.
- **Scroll results into view** with `show:top` on the form's swap
  (`index.html:17-23`) — one attribute, no JavaScript.
- **Aggregate progress** in the results header: `3 of 9 done`, using plan 08's
  listener.
- **Copy and collapse** per column, in `_columns.html` and
  `_column_final.html`.

### 9.5 Smaller fixes

- Show `showing 12 of 337` above the list; pass the unfiltered count from
  `model_list`. The placeholder count is currently static.
- Give `#results` an empty state describing what will appear there.
- Add helper text to `Max output tokens` tying it to the cost ceiling.
- Add `:focus-visible` rings to chips, buttons and rows.
- Add `aria-live="polite"` to `#estimate` and the results container.
- List chosen files with their extracted token counts, beside the existing
  extraction warnings in `_estimate.html:5-7`.

## Design notes

**The tray is the whole point.** Every other change here is worth doing, but
the reported confusion is fixed by making the selection visible and giving it
one home. If this plan is cut short, 9.1 is the part to keep.

**Removal round-trips to the server, deliberately.** The `×` could unset a
checkbox in JavaScript, but the selection already lives in the form and the
server already renders it. One source of truth is worth a request on a local
tool.

**Wrapping columns weakens "side by side" and that is the right trade.** Nine
columns are not comparable across 2900px of horizontal scroll either; they are
just hidden behind a scrollbar instead of a fold.

## Acceptance criteria

- Choosing a preset visibly fills the tray, updates the count, and updates the
  estimate.
- Typing in the search box changes the list and leaves the tray untouched.
- `×` removes a model from the tray and clears its checkbox in the list.
- A selected model hidden by a filter stays selected — the existing
  `hidden_selected` behaviour must survive.
- Prices read as per-million, with input and output both shown; free and
  variable-priced models are unaffected.
- The run bar stays visible while scrolling and shows the current totals.
- Nine models wrap into rows; no horizontal page scroll.
- Results scroll into view after a run, and progress counts up.

## Tests

Extend `tests/test_routes.py`:

- `/models?remove=…` drops exactly that id and keeps the rest
- the tray renders one chip per selected model
- the total count is rendered alongside the filtered count
- prices render per-million, and free and `-1` models still do not

The existing assertions on `sse-swap` names and escaped output stay as the
guardrail against template regressions.

## Out of scope

Comparison history, diffing answers, and the per-model provider override — the
`/models/{id}/endpoints` route exists but nothing in the UI uses it. Each is
its own plan.
