"""Typed access to the OpenRouter model catalog, with an in-memory TTL cache."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from time import monotonic
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from promptheus.config import Settings

logger = logging.getLogger(__name__)

_FROZEN = ConfigDict(extra="ignore", frozen=True)
"""Ignore unknown keys: the catalog grows new fields and must not break us."""


class Pricing(BaseModel):
    """Per-token prices, in USD.

    Values arrive as decimal strings (`"0.0000000938"`) and are kept as
    `Decimal`. A `float` here would quietly lose precision on figures this
    small, and the error would only show up in a bill.
    """

    model_config = _FROZEN

    prompt: Decimal
    completion: Decimal

    @property
    def is_variable(self) -> bool:
        """True for router models, which report `-1` until they have routed."""
        return self.prompt < 0 or self.completion < 0

    @property
    def is_free(self) -> bool:
        return self.prompt == 0 and self.completion == 0


class Architecture(BaseModel):
    model_config = _FROZEN

    input_modalities: list[str] = Field(default_factory=list)
    output_modalities: list[str] = Field(default_factory=list)
    tokenizer: str | None = None


class TopProvider(BaseModel):
    model_config = _FROZEN

    # Null for about an eighth of the catalog.
    max_completion_tokens: int | None = None


class Model(BaseModel):
    model_config = _FROZEN

    id: str
    name: str
    context_length: int
    pricing: Pricing
    architecture: Architecture = Field(default_factory=Architecture)
    top_provider: TopProvider = Field(default_factory=TopProvider)
    expiration_date: str | None = None

    @property
    def author(self) -> str:
        """The vendor prefix, e.g. `anthropic` for `anthropic/claude-opus-5`."""
        return self.id.split("/", 1)[0]

    @property
    def is_text_only_output(self) -> bool:
        return self.architecture.output_modalities == ["text"]

    def accepts(self, modality: str) -> bool:
        return modality in self.architecture.input_modalities


class Endpoint(BaseModel):
    """One provider serving one model."""

    model_config = _FROZEN

    provider_name: str
    pricing: Pricing
    context_length: int | None = None
    quantization: str | None = None


@dataclass(frozen=True)
class ModelFilters:
    """Optional narrowing applied on top of a text query."""

    author: str | None = None
    free_only: bool = False
    accepts_modality: str | None = None
    min_context: int | None = None
    text_output_only: bool = False

    def matches(self, model: Model) -> bool:
        if self.author is not None and model.author != self.author:
            return False
        if self.free_only and not model.pricing.is_free:
            return False
        if self.accepts_modality is not None and not model.accepts(self.accepts_modality):
            return False
        if self.min_context is not None and model.context_length < self.min_context:
            return False
        return not (self.text_output_only and not model.is_text_only_output)


class CatalogClient:
    """Thin HTTP wrapper over the catalog endpoints."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http

    def _headers(self) -> dict[str, str]:
        headers = {
            "HTTP-Referer": self._settings.openrouter_app_url,
            "X-OpenRouter-Title": self._settings.openrouter_app_title,
        }
        # `GET /models` is public. Send the key when we have one, but never
        # require it — browsing the catalog before configuring a key is a
        # legitimate first run.
        key = self._settings.openrouter_api_key
        if key is not None:
            headers["Authorization"] = f"Bearer {key.get_secret_value()}"
        return headers

    async def fetch_models(self) -> list[Model]:
        response = await self._http.get(
            f"{self._settings.openrouter_base_url}/models",
            headers=self._headers(),
        )
        response.raise_for_status()
        payload: Any = response.json()
        return _parse_models(payload)

    async def fetch_endpoints(self, model_id: str) -> list[Endpoint]:
        response = await self._http.get(
            f"{self._settings.openrouter_base_url}/models/{model_id}/endpoints",
            headers=self._headers(),
        )
        response.raise_for_status()
        payload: Any = response.json()
        raw_endpoints = (payload.get("data") or {}).get("endpoints") or []
        return _parse_each(raw_endpoints, Endpoint, "endpoint")


def _parse_models(payload: Any) -> list[Model]:
    models = _parse_each(payload.get("data") or [], Model, "model")
    # Models with an expiration date are on their way out; offering them only
    # sets the user up for a comparison that stops working.
    return [model for model in models if model.expiration_date is None]


def _parse_each[T: BaseModel](raw_items: Any, model_type: type[T], label: str) -> list[T]:
    """Parse a list, skipping entries that fail rather than losing all of them."""
    parsed: list[T] = []
    for raw in raw_items:
        try:
            parsed.append(model_type.model_validate(raw))
        except ValidationError as error:
            identifier = raw.get("id") if isinstance(raw, dict) else raw
            logger.warning("skipping malformed %s %r: %s", label, identifier, error)
    return parsed


class Catalog:
    """The catalog, cached in memory for `catalog_ttl_seconds`.

    Endpoints are fetched lazily, one model at a time: there are hundreds of
    models and almost nobody looks at the provider list for more than one.
    """

    def __init__(
        self,
        client: CatalogClient,
        ttl_seconds: float,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._clock = clock
        self._models: dict[str, Model] = {}
        self._fetched_at: float | None = None
        self._endpoints: dict[str, tuple[float, list[Endpoint]]] = {}
        # Guards the refresh so a burst of concurrent requests triggers one
        # fetch instead of one per request.
        self._lock = asyncio.Lock()

    def _is_fresh(self, fetched_at: float | None) -> bool:
        return fetched_at is not None and (self._clock() - fetched_at) < self._ttl

    async def _refresh_if_stale(self) -> None:
        if self._is_fresh(self._fetched_at):
            return
        async with self._lock:
            # Another coroutine may have refreshed while we waited.
            if self._is_fresh(self._fetched_at):
                return
            models = await self._client.fetch_models()
            self._models = {model.id: model for model in models}
            self._fetched_at = self._clock()

    async def all(self) -> list[Model]:
        await self._refresh_if_stale()
        return list(self._models.values())

    async def get(self, model_id: str) -> Model | None:
        await self._refresh_if_stale()
        return self._models.get(model_id)

    async def search(self, query: str = "", filters: ModelFilters | None = None) -> list[Model]:
        """Case-insensitive substring match on id and display name."""
        filters = filters or ModelFilters()
        needle = query.strip().lower()
        return [
            model
            for model in await self.all()
            if filters.matches(model)
            and (not needle or needle in model.id.lower() or needle in model.name.lower())
        ]

    async def endpoints(self, model_id: str) -> list[Endpoint]:
        cached = self._endpoints.get(model_id)
        if cached is not None and self._is_fresh(cached[0]):
            return cached[1]
        endpoints = await self._client.fetch_endpoints(model_id)
        self._endpoints[model_id] = (self._clock(), endpoints)
        return endpoints
