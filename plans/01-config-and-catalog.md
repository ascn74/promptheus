# 01 — Settings and model catalog

Load configuration, and fetch and cache the OpenRouter model catalog.

## Files

- `src/promptheus/config.py`
- `src/promptheus/catalog.py`
- `tests/test_catalog.py`

## Scope

**`config.py`** — a `Settings` class built on `pydantic-settings`, reading from
the environment and `.env`:

| Setting | Default | Notes |
|---|---|---|
| `openrouter_api_key` | *(required)* | never logged, never sent to the browser |
| `openrouter_base_url` | `https://openrouter.ai/api/v1` | |
| `openrouter_app_url` | `http://localhost:8000` | sent as `HTTP-Referer` |
| `openrouter_app_title` | `Promptheus` | sent as `X-OpenRouter-Title` |
| `catalog_ttl_seconds` | `3600` | |
| `request_timeout_seconds` | `600` | reasoning models are slow |

Use a cached `get_settings()` accessor so the app has one instance and tests
can override it.

**`catalog.py`** — typed access to the catalog:

- `Model` and `Pricing` as frozen pydantic models. Keep only what the app
  needs: `id`, `name`, `context_length`, `pricing.prompt`,
  `pricing.completion`, `architecture.input_modalities`,
  `architecture.tokenizer`, `top_provider.max_completion_tokens`.
- `Endpoint` for the per-provider view: `provider_name`, `context_length`,
  `pricing`, `quantization`.
- `CatalogClient.fetch_models()` → `GET /models`
- `CatalogClient.fetch_endpoints(model_id)` → `GET /models/{id}/endpoints`
- `Catalog` — in-memory cache over the client, with `catalog_ttl_seconds` TTL,
  exposing `all()`, `get(model_id)` and `search(query, filters)`.

## Design notes

**Prices are `Decimal`.** The API returns them as decimal strings per token
(`"0.0000000938"`). Parse straight from `str` to `Decimal`; never let a
`float` touch them. Declare the fields as `Decimal` and let pydantic coerce
from the string.

**`GET /models` needs no API key**, so `fetch_models` must work unauthenticated
— useful for tests and for the first run before a key is configured. Send the
key when there is one.

**Endpoints are fetched lazily**, one model at a time, only when the UI asks.
There are 345 models; fetching every model's endpoints up front is 345
requests for data that is almost never looked at.

**Filter out expired models.** Entries carrying a non-null `expiration_date`
are on their way out and should not be offered.

**Be tolerant of unknown fields.** The catalog gains fields over time; the
models must not reject a payload because something new showed up.

## Acceptance criteria

- `Catalog.all()` returns parsed models, second call inside the TTL makes no
  HTTP request, and a call after the TTL refetches.
- Prices are `Decimal` and exact — `str(model.pricing.prompt)` round-trips the
  input string.
- `search("claude")` matches on both id and display name, case-insensitively.
- A malformed or partial entry is skipped with a warning instead of taking down
  the whole catalog load.

## Tests

Use `respx` with a trimmed catalog fixture (about five models, including one
with an `expiration_date` and one missing optional fields). Freeze time to
exercise the TTL — no `sleep` in tests.

## Out of scope

Provider pinning in requests (plan 05) and any UI (plan 07).
