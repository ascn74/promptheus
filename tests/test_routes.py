"""Tests for the web interface. Nothing here touches the network."""

import json
import re
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from promptheus import app as app_module
from promptheus.config import Settings
from promptheus.openrouter import (
    CompletionError,
    Message,
    ReasoningDelta,
    RoutingOptions,
    StreamEvent,
    TextDelta,
    Usage,
)

BASE_URL = "https://openrouter.test/api/v1"
API_KEY = "sk-test-do-not-leak"

CATALOG: dict[str, Any] = {
    "data": [
        {
            "id": "anthropic/claude-opus-5",
            "name": "Claude Opus 5",
            "context_length": 1000000,
            "pricing": {"prompt": "0.000005", "completion": "0.000025"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        },
        {
            "id": "openai/gpt-5.6-sol",
            "name": "GPT-5.6 Sol",
            "context_length": 1050000,
            "pricing": {"prompt": "0.000005", "completion": "0.00003"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        },
        {
            "id": "google/gemma-4-31b-it:free",
            "name": "Gemma 4 31B (free)",
            "context_length": 262144,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        },
        {
            "id": "openrouter/auto",
            "name": "Auto Router",
            "context_length": 2000000,
            # Prices at request time, so -1 must never reach the screen.
            "pricing": {"prompt": "-1", "completion": "-1"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        },
    ]
}

PRESETS = """
[flagships]
label = "Flagships"
models = ["anthropic/claude-opus-5", "openai/gpt-5.6-sol"]

[anthropic]
label = "Anthropic"
models = ["anthropic/claude-opus-5"]
"""


class FakeCompletions:
    """Stands in for OpenRouterClient, scripted per model."""

    def __init__(self, scripts: dict[str, list[object]] | None = None) -> None:
        self.scripts = scripts or {}

    async def stream_completion(
        self,
        model_id: str,
        messages: Sequence[Message],
        routing: RoutingOptions | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        for step in self.scripts.get(model_id, [TextDelta("ok")]):
            if isinstance(step, Exception):
                raise step
            assert isinstance(step, TextDelta | ReasoningDelta | Usage)
            yield step


@pytest.fixture
def presets_file(tmp_path: Any) -> Any:
    path = tmp_path / "presets.toml"
    path.write_text(PRESETS, encoding="utf-8")
    return path


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, presets_file: Any) -> Iterator[TestClient]:
    settings = Settings(
        openrouter_api_key=SecretStr(API_KEY),
        openrouter_base_url=BASE_URL,
        presets_path=presets_file,
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)

    with respx.mock:
        respx.get(f"{BASE_URL}/models").mock(return_value=httpx.Response(200, json=CATALOG))
        application = app_module.create_app()
        with TestClient(application) as test_client:
            application.state.completions = FakeCompletions()
            yield test_client


def set_scripts(test_client: TestClient, scripts: dict[str, list[object]]) -> None:
    application: FastAPI = test_client.app  # type: ignore[assignment]
    application.state.completions = FakeCompletions(scripts)


def sse_events(body: str) -> list[tuple[str, str]]:
    """Parse an SSE body into (event name, joined data) pairs."""
    events: list[tuple[str, str]] = []
    name: str | None = None
    data: list[str] = []
    for line in body.split("\n"):
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            data.append(line[len("data: ") :])
        elif line == "" and name is not None:
            events.append((name, "\n".join(data)))
            name, data = None, []
    return events


def start_run(test_client: TestClient, models: list[str], prompt: str = "hello") -> str:
    response = test_client.post(
        "/runs",
        data={"prompt": prompt, "models": models},
        files={"files": ("notes.txt", b"attached body", "text/plain")},
    )
    assert response.status_code == 200
    run_id: str = response.text.split('sse-connect="/runs/')[1].split("/stream")[0]
    return run_id


# --- pages -------------------------------------------------------------------


def test_index_renders_with_presets(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Promptheus" in response.text
    assert "Flagships" in response.text
    assert "preset=flagships" in response.text


def test_index_vendors_its_javascript(client: TestClient) -> None:
    response = client.get("/")

    # A CDN reference would break the offline promise and unpin the version.
    assert "/static/htmx.min.js" in response.text
    assert "unpkg.com" not in response.text
    assert client.get("/static/htmx.min.js").status_code == 200


def test_model_search_filters_server_side(client: TestClient) -> None:
    response = client.get("/models", params={"q": "claude"})

    assert "anthropic/claude-opus-5" in response.text
    assert "openai/gpt-5.6-sol" not in response.text


def test_free_only_filter(client: TestClient) -> None:
    response = client.get("/models", params={"free_only": "true"})

    assert "gemma-4-31b-it:free" in response.text
    assert "anthropic/claude-opus-5" not in response.text


def test_a_preset_checks_its_models(client: TestClient) -> None:
    response = client.get("/models", params={"preset": "flagships"})

    assert response.text.count("checked") == 2


def test_selection_survives_a_search_that_hides_it(client: TestClient) -> None:
    response = client.get(
        "/models",
        params={"q": "gemma", "models": ["anthropic/claude-opus-5"]},
    )

    # Otherwise typing in the search box would silently drop the selection.
    assert '<input type="hidden" name="models" value="anthropic/claude-opus-5"' in response.text


def test_the_list_reports_how_much_it_is_hiding(client: TestClient) -> None:
    response = client.get("/models", params={"q": "claude"})

    # A static "337 models" placeholder tells you nothing once you have typed.
    assert "Showing 1 of 4 models" in response.text


def test_prices_read_per_million_not_per_token(client: TestClient) -> None:
    response = client.get("/models", params={"q": "claude"})

    # $0.000005/tok is unreadable; $5.00/M is how every vendor quotes it.
    assert "$5.00/M in" in response.text
    assert "$25.00/M out" in response.text
    assert "/tok" not in response.text


def visible_text(html: str) -> str:
    """Strip tags, so assertions cannot trip over class names in attributes."""
    return re.sub(r"<[^>]+>", " ", html)


def test_free_and_router_models_do_not_render_a_price(client: TestClient) -> None:
    free = visible_text(client.get("/models", params={"q": "gemma"}).text)
    router = visible_text(client.get("/models", params={"q": "auto"}).text)

    assert "free" in free
    assert "/M in" not in free
    assert "variable price" in router
    # A -1 rendered as money would be worse than an error: it looks plausible.
    assert "-1" not in router
    assert "/M in" not in router


# --- selection tray ----------------------------------------------------------


def test_the_tray_lists_every_selected_model(client: TestClient) -> None:
    response = client.get(
        "/selection",
        params={"models": ["anthropic/claude-opus-5", "openai/gpt-5.6-sol"]},
    )

    assert "2 models selected" in response.text
    assert "Claude Opus 5" in response.text
    assert "GPT-5.6 Sol" in response.text


def test_an_empty_tray_says_what_to_do(client: TestClient) -> None:
    response = client.get("/selection")

    assert "No models selected" in response.text
    assert "Clear all" not in response.text


def test_the_tray_offers_removal_for_each_model(client: TestClient) -> None:
    response = client.get("/selection", params={"models": ["anthropic/claude-opus-5"]})

    assert "/models?remove=anthropic/claude-opus-5" in response.text
    assert 'aria-label="Remove Claude Opus 5"' in response.text


def test_clear_all_does_not_resubmit_the_selection(client: TestClient) -> None:
    response = client.get("/selection", params={"models": ["anthropic/claude-opus-5"]})

    clear = next(line for line in response.text.splitlines() if "Clear all" in line)
    block = response.text[: response.text.index(clear)]
    include = re.findall(r'hx-include="([^"]*)"', block)[-1]

    # htmx inherits hx-include down the tree, so without an explicit one this
    # button would resubmit the very selection it is meant to clear.
    assert "models" not in include
    assert "q" in include


def test_the_tray_surfaces_ids_the_catalog_does_not_know(client: TestClient) -> None:
    response = client.get(
        "/selection",
        params={"models": ["anthropic/claude-opus-5", "vendor/retired"]},
    )

    # It still occupies a slot in the run, so it must not vanish silently.
    assert "vendor/retired" in response.text
    assert "not in the catalogue" in response.text
    assert "2 models selected" in response.text


def test_removing_a_model_drops_only_that_one(client: TestClient) -> None:
    response = client.get(
        "/models",
        params={
            "models": ["anthropic/claude-opus-5", "openai/gpt-5.6-sol"],
            "remove": "anthropic/claude-opus-5",
        },
    )

    checked = re.findall(r'value="([^"]+)"\s*checked', response.text)
    assert "anthropic/claude-opus-5" not in checked
    assert "openai/gpt-5.6-sol" in checked


def test_removing_a_model_not_selected_changes_nothing(client: TestClient) -> None:
    response = client.get(
        "/models",
        params={"models": ["openai/gpt-5.6-sol"], "remove": "not/selected"},
    )

    assert response.text.count("checked") == 1


# --- estimate ----------------------------------------------------------------


def test_estimate_prices_the_selection(client: TestClient) -> None:
    response = client.post(
        "/estimate",
        data={"prompt": "hello there", "models": ["anthropic/claude-opus-5"]},
    )

    assert response.status_code == 200
    assert "Claude Opus 5" in response.text
    assert "input tokens" in response.text


def test_estimate_with_no_models_is_not_an_error(client: TestClient) -> None:
    response = client.post("/estimate", data={"prompt": "hello"})

    assert response.status_code == 200
    assert "Select models" in response.text


def test_estimate_updates_the_run_bar_out_of_band(client: TestClient) -> None:
    response = client.post(
        "/estimate",
        data={"prompt": "hello", "models": ["anthropic/claude-opus-5"]},
    )

    # One render feeds the breakdown and the sticky bar, with no second request.
    assert 'id="runbar-summary" hx-swap-oob="true"' in response.text
    assert "1 model" in response.text
    assert "output up to" in response.text


def test_the_run_bar_reports_an_empty_selection(client: TestClient) -> None:
    response = client.post("/estimate", data={"prompt": "hello"})

    assert 'id="runbar-summary" hx-swap-oob="true"' in response.text
    assert "No models selected" in response.text


def test_attachments_are_listed_with_their_token_cost(client: TestClient) -> None:
    response = client.post(
        "/estimate",
        data={"prompt": "hi", "models": ["anthropic/claude-opus-5"]},
        files={"files": ("notes.txt", b"some attached prose here", "text/plain")},
    )

    # Attachments usually dominate the bill, so their size has to be visible.
    assert "notes.txt" in response.text
    assert "tok" in response.text


def test_estimate_surfaces_attachment_warnings(client: TestClient) -> None:
    response = client.post(
        "/estimate",
        data={"prompt": "hi", "models": ["anthropic/claude-opus-5"]},
        files={"files": ("scan.pdf", b"%PDF-1.4\nbroken", "application/pdf")},
    )

    assert "scan.pdf" in response.text


# --- runs --------------------------------------------------------------------


def test_creating_a_run_returns_one_column_per_model(client: TestClient) -> None:
    response = client.post(
        "/runs",
        data={"prompt": "hi", "models": ["anthropic/claude-opus-5", "openai/gpt-5.6-sol"]},
    )

    assert response.status_code == 200
    assert 'sse-swap="m0"' in response.text
    assert 'sse-swap="m1"' in response.text
    assert 'sse-connect="/runs/' in response.text
    # Model ids must not appear as event names: they carry / and .
    assert 'sse-swap="anthropic/claude-opus-5"' not in response.text


def test_a_run_needs_at_least_one_model(client: TestClient) -> None:
    assert client.post("/runs", data={"prompt": "hi"}).status_code == 400


def test_unknown_run_id_is_404(client: TestClient) -> None:
    assert client.get("/runs/nope/stream").status_code == 404


# --- stream ------------------------------------------------------------------


def test_stream_emits_named_events_per_column(client: TestClient) -> None:
    set_scripts(
        client,
        {
            "anthropic/claude-opus-5": [TextDelta("Hello "), TextDelta("world")],
            "openai/gpt-5.6-sol": [TextDelta("Hi")],
        },
    )
    run_id = start_run(client, ["anthropic/claude-opus-5", "openai/gpt-5.6-sol"])

    response = client.get(f"/runs/{run_id}/stream")

    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response.text)
    names = [name for name, _ in events]
    assert "m0" in names
    assert "m1" in names
    assert "m0-done" in names
    assert "m1-done" in names
    assert names[-1] == "run-finished"


def test_stream_renders_markdown_at_the_end(client: TestClient) -> None:
    set_scripts(client, {"anthropic/claude-opus-5": [TextDelta("# Title\n\nsome **bold**")]})
    run_id = start_run(client, ["anthropic/claude-opus-5"])

    events = sse_events(client.get(f"/runs/{run_id}/stream").text)
    final = next(data for name, data in events if name == "m0-done")

    assert "<h1>Title</h1>" in final
    assert "<strong>bold</strong>" in final


def test_model_output_cannot_inject_script_while_streaming(client: TestClient) -> None:
    set_scripts(client, {"anthropic/claude-opus-5": [TextDelta("<script>alert(1)</script>")]})
    run_id = start_run(client, ["anthropic/claude-opus-5"])

    events = sse_events(client.get(f"/runs/{run_id}/stream").text)
    delta = next(data for name, data in events if name == "m0")

    assert "<script>" not in delta
    assert "&lt;script&gt;" in delta


def test_model_output_cannot_inject_script_in_rendered_markdown(client: TestClient) -> None:
    # The commonmark preset allows raw HTML; this proves it is switched off.
    set_scripts(
        client, {"anthropic/claude-opus-5": [TextDelta("text\n\n<script>alert(1)</script>")]}
    )
    run_id = start_run(client, ["anthropic/claude-opus-5"])

    events = sse_events(client.get(f"/runs/{run_id}/stream").text)
    final = next(data for name, data in events if name == "m0-done")

    assert "<script>" not in final


def test_newlines_survive_the_sse_framing(client: TestClient) -> None:
    set_scripts(client, {"anthropic/claude-opus-5": [TextDelta("line one\nline two")]})
    run_id = start_run(client, ["anthropic/claude-opus-5"])

    events = sse_events(client.get(f"/runs/{run_id}/stream").text)
    delta = next(data for name, data in events if name == "m0")

    # A raw newline would have ended the SSE message early.
    assert delta == "line one\nline two"


def test_reasoning_arrives_on_its_own_event(client: TestClient) -> None:
    set_scripts(
        client,
        {"anthropic/claude-opus-5": [ReasoningDelta("thinking"), TextDelta("answer")]},
    )
    run_id = start_run(client, ["anthropic/claude-opus-5"])

    events = sse_events(client.get(f"/runs/{run_id}/stream").text)
    names = [name for name, _ in events]

    assert "m0-reasoning" in names
    assert "m0" in names


def test_one_failing_model_does_not_break_the_others(client: TestClient) -> None:
    set_scripts(
        client,
        {
            "anthropic/claude-opus-5": [
                CompletionError("anthropic/claude-opus-5", "upstream died")
            ],
            "openai/gpt-5.6-sol": [TextDelta("fine")],
        },
    )
    run_id = start_run(client, ["anthropic/claude-opus-5", "openai/gpt-5.6-sol"])

    events = dict(sse_events(client.get(f"/runs/{run_id}/stream").text))

    assert "upstream died" in events["m0-done"]
    assert "failed" in events["m0-done"]
    assert "fine" in events["m1-done"]


def test_usage_is_reported_on_the_finished_column(client: TestClient) -> None:
    set_scripts(
        client,
        {
            "anthropic/claude-opus-5": [
                TextDelta("hi"),
                Usage(prompt_tokens=10, completion_tokens=5),
            ]
        },
    )
    run_id = start_run(client, ["anthropic/claude-opus-5"])

    events = dict(sse_events(client.get(f"/runs/{run_id}/stream").text))

    assert "5 tok" in events["m0-done"]


def test_a_model_that_returns_nothing_says_so(client: TestClient) -> None:
    set_scripts(client, {"anthropic/claude-opus-5": [ReasoningDelta("thought only")]})
    run_id = start_run(client, ["anthropic/claude-opus-5"])

    events = dict(sse_events(client.get(f"/runs/{run_id}/stream").text))

    assert "no text" in events["m0-done"]


def test_reasoning_survives_into_the_finished_column(client: TestClient) -> None:
    set_scripts(
        client,
        {"anthropic/claude-opus-5": [ReasoningDelta("weighing it up"), TextDelta("answer")]},
    )
    run_id = start_run(client, ["anthropic/claude-opus-5"])

    events = dict(sse_events(client.get(f"/runs/{run_id}/stream").text))

    # The finished column replaces the streaming one, so the reasoning has to
    # be re-rendered or it is lost.
    assert "weighing it up" in events["m0-done"]


def test_a_reasoning_only_answer_opens_its_reasoning(client: TestClient) -> None:
    set_scripts(client, {"anthropic/claude-opus-5": [ReasoningDelta("all budget spent here")]})
    run_id = start_run(client, ["anthropic/claude-opus-5"])

    events = dict(sse_events(client.get(f"/runs/{run_id}/stream").text))

    # When thinking is all there is, it should not be hidden behind a click.
    assert "all budget spent here" in events["m0-done"]
    assert '<details class="reasoning" open>' in events["m0-done"]


def test_the_attachment_reaches_the_models(client: TestClient) -> None:
    seen: list[str] = []

    class Recorder(FakeCompletions):
        async def stream_completion(
            self,
            model_id: str,
            messages: Sequence[Message],
            routing: RoutingOptions | None = None,
            max_tokens: int | None = None,
        ) -> AsyncIterator[StreamEvent]:
            seen.append(messages[0]["content"])
            yield TextDelta("ok")

    application: FastAPI = client.app  # type: ignore[assignment]
    application.state.completions = Recorder()
    run_id = start_run(client, ["anthropic/claude-opus-5"], prompt="Summarise")

    client.get(f"/runs/{run_id}/stream")

    assert "Summarise" in seen[0]
    assert '<attachment name="notes.txt">' in seen[0]
    assert "attached body" in seen[0]


# --- secrets -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/"), ("get", "/models"), ("post", "/estimate"), ("post", "/runs")],
)
def test_no_route_leaks_the_api_key(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path, data={"models": ["anthropic/claude-opus-5"]})

    assert API_KEY not in response.text


def test_the_stream_does_not_leak_the_api_key(client: TestClient) -> None:
    run_id = start_run(client, ["anthropic/claude-opus-5"])

    assert API_KEY not in client.get(f"/runs/{run_id}/stream").text


def test_the_catalog_json_is_never_shipped_to_the_browser(client: TestClient) -> None:
    response = client.get("/")

    # Filtering is server-side; the browser gets HTML, not the catalogue.
    assert json.dumps(CATALOG) not in response.text
