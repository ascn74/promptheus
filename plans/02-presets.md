# 02 — Presets

Load the named model sets from `presets.toml` and validate them against the
live catalog.

**Depends on:** 01

## Files

- `src/promptheus/presets.py`
- `tests/test_presets.py`

## Scope

- `Preset` — `key`, `label`, `models: list[str]`.
- `load_presets(path)` — parses the file with `tomllib` (standard library, no
  dependency needed) and returns `dict[str, Preset]` preserving file order.
- `validate(presets, catalog)` — returns the presets alongside a list of
  warnings for ids absent from the catalog.

## Design notes

**Unknown ids warn, they do not crash.** Vendors retire models; a stale id in
`presets.toml` must not stop the app from starting. Drop the id from the
preset, record a warning, carry on. The UI shows these warnings once so the
user knows to edit the file.

**`label` is optional** — fall back to the section key, title-cased.

**The file path is a setting** (`presets_path`, defaulting to `presets.toml` at
the repository root) so tests can point at a fixture.

## Acceptance criteria

- All ten presets in the shipped `presets.toml` load, in file order, with
  `flagships` first.
- An id not in the catalog is dropped and reported; the rest of that preset
  survives.
- A preset left empty after validation is dropped entirely, with a warning.
- A malformed TOML file raises a clear error naming the file and the problem.

## Tests

Fixture TOML files: one valid, one with an unknown id, one malformed. Use a
fake catalog rather than a real fetch.

## Out of scope

Editing presets from the UI — the file is edited by hand, by design.
