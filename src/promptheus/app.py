"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from promptheus.catalog import Catalog, CatalogClient
from promptheus.config import get_settings
from promptheus.openrouter import OpenRouterClient
from promptheus.orchestrator import RunRegistry
from promptheus.routes import router

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the HTTP client for the process lifetime.

    One client means one connection pool shared by the catalog and by every
    concurrent completion, instead of a fresh pool per request.
    """
    settings = get_settings()
    async with httpx.AsyncClient() as http:
        app.state.settings = settings
        app.state.catalog = Catalog(
            CatalogClient(settings, http),
            settings.catalog_ttl_seconds,
        )
        app.state.completions = OpenRouterClient(settings, http)
        app.state.runs = RunRegistry(ttl_seconds=settings.run_ttl_seconds)
        yield


def create_app() -> FastAPI:
    app = FastAPI(title="Promptheus", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(router)
    return app
