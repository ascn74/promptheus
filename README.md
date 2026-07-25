# Promptheus

> *Prometheus stole fire from the gods and gave it to mankind.*

Send one prompt and a set of files to several [OpenRouter](https://openrouter.ai)
models at once, and read the answers side by side.

## Why

Comparing model outputs usually means pasting the same prompt into several tabs
and losing track of which answer came from where — and of what it cost.
Promptheus fans a single request out to every model you selected, streams all
the answers into parallel columns as they arrive, and tells you the price
before you spend anything.

## Status

**Working.** All seven plans under [`plans/done/`](plans/done/) are
implemented: browse and price 300+ models, attach documents, and stream every
answer side by side.

## Features

- **One prompt, N models** — fanned out concurrently, answers streamed live
- **Cost estimate up front** — per model and total, before the request is sent
- **Attachments** — plain text, source code, PDF and DOCX, extracted to text
- **Presets** — named model sets in [`presets.toml`](presets.toml), so you are
  not picking from 345 models every time
- **Server-side API key** — the OpenRouter key never reaches the browser

## Design

A single FastAPI process serving Jinja templates driven by
[htmx](https://htmx.org). All models stream over **one multiplexed SSE
connection**: each event is named after the column it belongs to, so htmx
appends it to the right place with no custom JavaScript. This also sidesteps
the ~6-connections-per-origin limit browsers impose on HTTP/1.1, which one
stream per model would hit as soon as you selected seven.

Attachments are always reduced to text, even for models that accept PDF
natively — otherwise the models would not be reading the same input, and the
comparison would not be fair.

## Requirements

- Python 3.12+
- An [OpenRouter API key](https://openrouter.ai/keys)

## Setup

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate && pip install -e '.[dev]'
```

```bash
cp .env.example .env   # then fill in OPENROUTER_API_KEY
```

## Running

```bash
uvicorn promptheus.app:create_app --factory --reload
```

Then open http://127.0.0.1:8000.

## Development

```bash
ruff check . && ruff format --check . && mypy && pytest
```

The stylesheet is built from `src/promptheus/static/input.css` with Tailwind.
Rebuild it after changing any template or the input file, and commit the
result — the served `app.css` is a committed build artefact, so the app needs
no toolchain at runtime and works offline:

```bash
scripts/build-css.sh
```

The script downloads a pinned Tailwind standalone binary into `.tailwind/` on
first use. No Node is involved, and CI fails if the committed CSS does not
match a fresh build.

Contributions follow the plans in [`plans/`](plans/): each feature has a
document describing its scope and acceptance criteria, and moves to
`plans/done/` once implemented.

## License

MIT — see [LICENSE](LICENSE).
