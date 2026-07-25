"""Tests for the model catalog. No test here touches the network."""

from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from promptheus.catalog import Catalog, CatalogClient, ModelFilters
from promptheus.config import Settings

BASE_URL = "https://openrouter.test/api/v1"


class IsolatedSettings(Settings):
    """Settings built purely from explicit arguments.

    Without this, a developer with `OPENROUTER_API_KEY` exported — or a local
    `.env` — would leak a real key into the tests, and the assertion that no
    `Authorization` header is sent would pass or fail depending on the machine.
    """

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def make_settings(api_key: str | None = None) -> Settings:
    return IsolatedSettings(
        openrouter_api_key=SecretStr(api_key) if api_key else None,
        openrouter_base_url=BASE_URL,
        openrouter_app_url="http://localhost:8000",
        openrouter_app_title="Promptheus",
    )


def model_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "anthropic/claude-opus-5",
        "name": "Claude Opus 5",
        "context_length": 1000000,
        "pricing": {"prompt": "0.000005", "completion": "0.000025"},
        "architecture": {
            "input_modalities": ["text", "image", "file"],
            "output_modalities": ["text"],
            "tokenizer": "Claude",
        },
        "top_provider": {"max_completion_tokens": 128000},
    }
    payload.update(overrides)
    return payload


CATALOG: dict[str, Any] = {
    "data": [
        model_payload(),
        model_payload(
            id="deepseek/deepseek-v4-flash",
            name="DeepSeek V4 Flash",
            context_length=1048576,
            # Deliberately tiny: the kind of number a float would mangle.
            pricing={"prompt": "0.0000000938", "completion": "0.0000001876"},
            architecture={"input_modalities": ["text"], "output_modalities": ["text"]},
            # No top_provider at all — 45 real entries look like this.
            top_provider={},
        ),
        model_payload(
            id="google/gemma-4-31b-it:free",
            name="Gemma 4 31B (free)",
            context_length=262144,
            pricing={"prompt": "0", "completion": "0"},
        ),
        model_payload(
            id="openrouter/auto",
            name="Auto Router",
            context_length=2000000,
            # Router models report -1: the price is unknown until they route.
            pricing={"prompt": "-1", "completion": "-1"},
        ),
        model_payload(
            id="poolside/laguna-m.1",
            name="Laguna M.1",
            expiration_date="2026-08-01",
        ),
        # Malformed: context_length is required and missing.
        {"id": "broken/model", "name": "Broken", "pricing": {"prompt": "0", "completion": "0"}},
    ]
}

LIVE_MODEL_IDS = {
    "anthropic/claude-opus-5",
    "deepseek/deepseek-v4-flash",
    "google/gemma-4-31b-it:free",
    "openrouter/auto",
}


class FakeClock:
    """A clock the tests move by hand, so the TTL is exercised without sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


async def build_catalog(
    clock: FakeClock,
    ttl_seconds: float = 3600.0,
    api_key: str | None = None,
) -> tuple[Catalog, httpx.AsyncClient]:
    settings = make_settings(api_key)
    http = httpx.AsyncClient()
    return Catalog(CatalogClient(settings, http), ttl_seconds, clock), http


@respx.mock
async def test_parses_models_and_skips_the_malformed_one(clock: FakeClock) -> None:
    respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(200, json=CATALOG))
    catalog, http = await build_catalog(clock)

    async with http:
        models = await catalog.all()

    assert {model.id for model in models} == LIVE_MODEL_IDS


@respx.mock
async def test_expired_models_are_not_offered(clock: FakeClock) -> None:
    respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(200, json=CATALOG))
    catalog, http = await build_catalog(clock)

    async with http:
        models = await catalog.all()

    assert all(model.expiration_date is None for model in models)
    assert "poolside/laguna-m.1" not in {model.id for model in models}


@respx.mock
async def test_prices_keep_full_precision(clock: FakeClock) -> None:
    respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(200, json=CATALOG))
    catalog, http = await build_catalog(clock)

    async with http:
        model = await catalog.get("deepseek/deepseek-v4-flash")

    assert model is not None
    # Value equality, not string equality: str(Decimal("0.0000000938")) is
    # "9.38E-8", which round-trips as a value but not as text.
    assert model.pricing.prompt == Decimal("0.0000000938")
    assert model.pricing.completion == Decimal("0.0000001876")
    assert Decimal(str(model.pricing.prompt)) == Decimal("0.0000000938")


@respx.mock
async def test_router_models_are_flagged_as_variable_priced(clock: FakeClock) -> None:
    respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(200, json=CATALOG))
    catalog, http = await build_catalog(clock)

    async with http:
        router = await catalog.get("openrouter/auto")
        normal = await catalog.get("anthropic/claude-opus-5")

    assert router is not None and normal is not None
    assert router.pricing.is_variable
    assert not normal.pricing.is_variable


@respx.mock
async def test_missing_top_provider_defaults_instead_of_failing(clock: FakeClock) -> None:
    respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(200, json=CATALOG))
    catalog, http = await build_catalog(clock)

    async with http:
        model = await catalog.get("deepseek/deepseek-v4-flash")

    assert model is not None
    assert model.top_provider.max_completion_tokens is None


@respx.mock
async def test_unknown_fields_do_not_break_parsing(clock: FakeClock) -> None:
    payload = {"data": [model_payload(some_field_invented_next_quarter={"nested": True})]}
    respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(200, json=payload))
    catalog, http = await build_catalog(clock)

    async with http:
        models = await catalog.all()

    assert len(models) == 1


@respx.mock
async def test_catalog_is_cached_for_the_ttl(clock: FakeClock) -> None:
    route = respx.get(f"{BASE_URL}/models").mock(
        return_value=httpx.Response(200, json=CATALOG),
    )
    catalog, http = await build_catalog(clock, ttl_seconds=3600.0)

    async with http:
        await catalog.all()
        clock.advance(3599.0)
        await catalog.all()

    assert route.call_count == 1


@respx.mock
async def test_catalog_refetches_once_the_ttl_expires(clock: FakeClock) -> None:
    route = respx.get(f"{BASE_URL}/models").mock(
        return_value=httpx.Response(200, json=CATALOG),
    )
    catalog, http = await build_catalog(clock, ttl_seconds=3600.0)

    async with http:
        await catalog.all()
        clock.advance(3601.0)
        await catalog.all()

    assert route.call_count == 2


@respx.mock
async def test_concurrent_callers_trigger_a_single_fetch(clock: FakeClock) -> None:
    import asyncio

    route = respx.get(f"{BASE_URL}/models").mock(
        return_value=httpx.Response(200, json=CATALOG),
    )
    catalog, http = await build_catalog(clock)

    async with http:
        await asyncio.gather(*(catalog.all() for _ in range(10)))

    assert route.call_count == 1


@respx.mock
async def test_search_matches_id_and_name_case_insensitively(clock: FakeClock) -> None:
    respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(200, json=CATALOG))
    catalog, http = await build_catalog(clock)

    async with http:
        by_id = await catalog.search("DEEPSEEK")
        by_name = await catalog.search("opus")
        no_match = await catalog.search("nothing-like-this")
        everything = await catalog.search("")

    assert {model.id for model in by_id} == {"deepseek/deepseek-v4-flash"}
    assert {model.id for model in by_name} == {"anthropic/claude-opus-5"}
    assert no_match == []
    assert len(everything) == len(LIVE_MODEL_IDS)


@respx.mock
async def test_filters_narrow_the_result(clock: FakeClock) -> None:
    respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(200, json=CATALOG))
    catalog, http = await build_catalog(clock)

    async with http:
        free = await catalog.search(filters=ModelFilters(free_only=True))
        anthropic = await catalog.search(filters=ModelFilters(author="anthropic"))
        vision = await catalog.search(filters=ModelFilters(accepts_modality="image"))
        roomy = await catalog.search(filters=ModelFilters(min_context=1_000_000))

    assert {model.id for model in free} == {"google/gemma-4-31b-it:free"}
    assert {model.id for model in anthropic} == {"anthropic/claude-opus-5"}
    assert "deepseek/deepseek-v4-flash" not in {model.id for model in vision}
    assert {model.id for model in roomy} == {
        "anthropic/claude-opus-5",
        "deepseek/deepseek-v4-flash",
        "openrouter/auto",
    }


@respx.mock
async def test_endpoints_are_fetched_lazily_and_then_cached(clock: FakeClock) -> None:
    catalog_route = respx.get(f"{BASE_URL}/models").mock(
        return_value=httpx.Response(200, json=CATALOG),
    )
    endpoints_route = respx.get(f"{BASE_URL}/models/deepseek/deepseek-v4-flash/endpoints").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "id": "deepseek/deepseek-v4-flash",
                    "endpoints": [
                        {
                            "provider_name": "Novita",
                            "context_length": 163840,
                            "pricing": {"prompt": "0.00000027", "completion": "0.00000041"},
                            "quantization": "fp8",
                        },
                    ],
                }
            },
        ),
    )
    catalog, http = await build_catalog(clock)

    async with http:
        await catalog.all()
        assert endpoints_route.call_count == 0  # loading the catalog must not fetch these

        first = await catalog.endpoints("deepseek/deepseek-v4-flash")
        second = await catalog.endpoints("deepseek/deepseek-v4-flash")

    assert catalog_route.call_count == 1
    assert endpoints_route.call_count == 1
    assert first == second
    assert first[0].provider_name == "Novita"
    assert first[0].quantization == "fp8"
    assert first[0].pricing.prompt == Decimal("0.00000027")


@respx.mock
async def test_catalog_works_without_an_api_key(clock: FakeClock) -> None:
    route = respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(200, json=CATALOG))
    catalog, http = await build_catalog(clock, api_key=None)

    async with http:
        await catalog.all()

    assert "Authorization" not in route.calls.last.request.headers


@respx.mock
async def test_api_key_is_sent_when_configured(clock: FakeClock) -> None:
    route = respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(200, json=CATALOG))
    catalog, http = await build_catalog(clock, api_key="sk-test-123")

    async with http:
        await catalog.all()

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer sk-test-123"
    assert request.headers["X-OpenRouter-Title"] == "Promptheus"


@respx.mock
async def test_http_errors_propagate(clock: FakeClock) -> None:
    respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(500))
    catalog, http = await build_catalog(clock)

    async with http:
        with pytest.raises(httpx.HTTPStatusError):
            await catalog.all()
