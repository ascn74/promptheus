"""HTTP endpoints and the multiplexed SSE stream."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from promptheus.attachments import Attachment, extract
from promptheus.catalog import Catalog, Model, ModelFilters
from promptheus.config import Settings
from promptheus.estimate import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    count_tokens,
    estimate_run,
    format_usd,
)
from promptheus.openrouter import OpenRouterClient
from promptheus.orchestrator import Column, Run, RunRegistry, execute
from promptheus.presets import PresetError, PresetsResult, resolve_presets

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.filters["usd"] = format_usd
# Per-token prices are unreadable: nobody compares $0.000005 against
# $0.0000009 at a glance. Per million is the convention every vendor quotes.
templates.env.filters["per_million"] = lambda price: format_usd(price * 1_000_000)

# `html=False` matters: the commonmark preset enables raw HTML, which would let
# a model's answer inject <script> straight into the page.
markdown = MarkdownIt("commonmark", {"html": False, "linkify": False})


# --- application state -------------------------------------------------------


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _catalog(request: Request) -> Catalog:
    catalog: Catalog = request.app.state.catalog
    return catalog


def _runs(request: Request) -> RunRegistry:
    runs: RunRegistry = request.app.state.runs
    return runs


def _completions(request: Request) -> OpenRouterClient:
    client: OpenRouterClient = request.app.state.completions
    return client


# --- helpers -----------------------------------------------------------------


async def _load_catalog(request: Request) -> tuple[list[Model], str | None]:
    """Fetch the catalog, turning a failure into a banner rather than a 500."""
    try:
        return await _catalog(request).all(), None
    except Exception as error:
        logger.exception("could not load the model catalog")
        return [], f"Could not load the model catalog: {error}"


async def _load_presets(request: Request) -> tuple[PresetsResult, str | None]:
    settings = _settings(request)
    try:
        return await resolve_presets(settings.presets_path, _catalog(request)), None
    except PresetError as error:
        return PresetsResult(presets={}), str(error)
    except Exception as error:
        logger.exception("could not resolve presets")
        return PresetsResult(presets={}), f"Could not resolve presets: {error}"


@lru_cache(maxsize=8)
def _extract_cached(filename: str, data: bytes, max_chars: int) -> Attachment:
    """Extraction is re-run on every keystroke while estimating.

    Caching on the exact bytes keeps a large PDF from being parsed again each
    time the prompt changes.
    """
    return extract(filename, data, max_chars)


async def _read_attachments(files: Sequence[UploadFile], max_chars: int) -> list[Attachment]:
    attachments: list[Attachment] = []
    for upload in files:
        data = await upload.read()
        if not data:
            continue
        attachments.append(_extract_cached(upload.filename or "attachment", data, max_chars))
    return attachments


async def _resolve_models(request: Request, model_ids: Sequence[str]) -> list[Model]:
    catalog = _catalog(request)
    resolved = [await catalog.get(model_id) for model_id in model_ids]
    return [model for model in resolved if model is not None]


def _sse(event_name: str, data: str) -> str:
    """Frame one SSE message.

    Data is split across `data:` lines because a raw newline would end the
    message early; the browser rejoins them with newlines.
    """
    body = "".join(f"data: {line}\n" for line in data.split("\n"))
    return f"event: {event_name}\n{body}\n"


# --- pages -------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    models, catalog_error = await _load_catalog(request)
    presets, preset_error = await _load_presets(request)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "models": sorted(models, key=lambda model: model.id),
            "presets": presets.presets.values(),
            "warnings": presets.warnings,
            "errors": [error for error in (catalog_error, preset_error) if error],
            "selected": set(),
            "has_api_key": _settings(request).openrouter_api_key is not None,
            "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        },
    )


@router.get("/models", response_class=HTMLResponse)
async def model_list(
    request: Request,
    q: str = "",
    models: Annotated[list[str], Query()] = [],  # noqa: B006 - FastAPI reads the default
    preset: str | None = None,
    free_only: bool = False,
    remove: str | None = None,
) -> HTMLResponse:
    """Return the filtered model list.

    Filtering happens here rather than in the browser: the catalog is hundreds
    of models and none of it needs to be shipped to the client.
    """
    selected = set(models)
    if preset:
        presets, _ = await _load_presets(request)
        chosen = presets.presets.get(preset)
        if chosen:
            # Presets add to the selection rather than replacing it, so two
            # families can be combined by clicking both.
            selected |= set(chosen.models)
    if remove:
        # Lets the tray's remove button drop a model without any client-side
        # state: the selection lives in the form, and re-rendering the list
        # unchecks it.
        selected.discard(remove)

    catalog = _catalog(request)
    try:
        everything = await catalog.all()
        visible = await catalog.search(q, ModelFilters(free_only=free_only))
    except Exception:
        logger.exception("model search failed")
        everything, visible = [], []

    visible = sorted(visible, key=lambda model: model.id)
    visible_ids = {model.id for model in visible}

    return templates.TemplateResponse(
        request,
        "_model_list.html",
        {
            "models": visible,
            "total": len(everything),
            "selected": selected,
            # Selected models filtered out by the search still have to be
            # submitted, or searching would silently drop the selection.
            "hidden_selected": sorted(selected - visible_ids),
        },
    )


@router.get("/selection", response_class=HTMLResponse)
async def selection(
    request: Request,
    models: Annotated[list[str], Query()] = [],  # noqa: B006 - FastAPI reads the default
) -> HTMLResponse:
    """Render the tray of currently selected models.

    Its own endpoint rather than a slice of `/estimate`: ticking a checkbox has
    to update the tray, and the tray is about selection, not cost. Takes only
    the model ids, so it never re-uploads the attachments.
    """
    resolved = await _resolve_models(request, models)
    known = {model.id for model in resolved}

    return templates.TemplateResponse(
        request,
        "_selection.html",
        {
            "models": resolved,
            # Ids the catalog does not recognise still occupy a slot in the
            # run, so show them rather than quietly dropping them.
            "unknown": sorted(set(models) - known),
        },
    )


@router.post("/estimate", response_class=HTMLResponse)
async def estimate_endpoint(
    request: Request,
    prompt: Annotated[str, Form()] = "",
    models: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI reads the default
    files: Annotated[list[UploadFile], File()] = [],  # noqa: B006 - FastAPI reads the default
    max_output_tokens: Annotated[int, Form()] = DEFAULT_MAX_OUTPUT_TOKENS,
) -> HTMLResponse:
    settings = _settings(request)
    attachments = await _read_attachments(files, settings.max_attachment_chars)
    resolved = await _resolve_models(request, models)
    estimate = estimate_run(prompt, attachments, resolved, max_output_tokens)

    return templates.TemplateResponse(
        request,
        "_estimate.html",
        {
            "estimate": estimate,
            "attachments": attachments,
            "names": {model.id: model.name for model in resolved},
            # Attachments usually dominate the bill, so show what each one
            # actually costs in tokens rather than only its name.
            "attachment_tokens": {
                attachment.filename: count_tokens(attachment.text) for attachment in attachments
            },
        },
    )


@router.post("/runs", response_class=HTMLResponse)
async def create_run(
    request: Request,
    prompt: Annotated[str, Form()] = "",
    models: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI reads the default
    files: Annotated[list[UploadFile], File()] = [],  # noqa: B006 - FastAPI reads the default
    max_output_tokens: Annotated[int, Form()] = DEFAULT_MAX_OUTPUT_TOKENS,
) -> HTMLResponse:
    """Accept the upload and hand back the empty columns.

    Two requests are unavoidable: SSE is a GET, so the files cannot ride on the
    request that streams the answers.
    """
    if not models:
        raise HTTPException(status_code=400, detail="Select at least one model")

    settings = _settings(request)
    attachments = await _read_attachments(files, settings.max_attachment_chars)
    run = _runs(request).create(prompt, attachments, models, max_output_tokens=max_output_tokens)
    names = {model.id: model.name for model in await _resolve_models(request, models)}

    return templates.TemplateResponse(
        request,
        "_columns.html",
        {"run": run, "names": names},
    )


@router.get("/runs/{run_id}/stream")
async def stream_run(request: Request, run_id: str) -> StreamingResponse:
    run = _runs(request).get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Unknown or expired run")

    return StreamingResponse(
        _stream_events(request, run),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Stops nginx-style proxies from buffering the stream into silence.
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_events(request: Request, run: Run) -> AsyncIterator[str]:
    """Emit one named event per column over a single connection.

    Naming each event after its column is what keeps this to one connection.
    A stream per model would hit the browser's ~6-per-origin cap on HTTP/1.1
    and stall the seventh column with no error anywhere.
    """
    client = _completions(request)
    answers: dict[str, list[str]] = {column.slug: [] for column in run.columns}
    # Kept because the finished column replaces the streaming one wholesale.
    # Without this the reasoning is discarded exactly when it is the only
    # thing a model produced.
    thoughts: dict[str, list[str]] = {column.slug: [] for column in run.columns}
    names = {model.id: model.name for model in await _resolve_models(request, run.model_ids)}

    async with aclosing(execute(run, client)) as events:
        async for event in events:
            column = run.column_for(event.slug)
            if column is None:
                continue

            if event.type == "delta":
                answers[event.slug].append(event.text)
                # Escaped: model output is untrusted and goes straight into
                # the DOM.
                yield _sse(event.slug, escape(event.text))

            elif event.type == "reasoning":
                thoughts[event.slug].append(event.text)
                yield _sse(f"{event.slug}-reasoning", escape(event.text))

            elif event.type in ("done", "error"):
                yield _sse(
                    f"{event.slug}-done",
                    _render_final_column(request, run, column, names, answers, thoughts, event),
                )

    yield _sse("run-finished", "")


def _render_final_column(
    request: Request,
    run: Run,
    column: Column,
    names: dict[str, str],
    answers: dict[str, list[str]],
    thoughts: dict[str, list[str]],
    event: Any,
) -> str:
    """Render the finished column, markdown and all.

    Markdown is rendered once, here, rather than on every delta: re-parsing a
    half-written code fence dozens of times a second is slower and looks worse.
    """
    text = "".join(answers[column.slug])
    return templates.get_template("_column_final.html").render(
        {
            "request": request,
            "run": run,
            "column": column,
            "name": names.get(column.model_id, column.model_id),
            "failed": event.type == "error",
            "message": event.text,
            "usage": event.usage,
            "body": markdown.render(text) if text.strip() else "",
            "empty": not text.strip(),
            "reasoning": "".join(thoughts[column.slug]).strip(),
        }
    )
