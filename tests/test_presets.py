"""Tests for preset loading and validation."""

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from promptheus.catalog import Catalog, CatalogClient
from promptheus.config import Settings
from promptheus.presets import (
    Preset,
    PresetError,
    load_presets,
    resolve_presets,
    validate_presets,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_PRESETS = REPO_ROOT / "presets.toml"

VALID = """
[flagships]
label = "Flagships"
models = ["anthropic/claude-opus-5", "openai/gpt-5.6-sol"]

[anthropic]
label = "Anthropic — Claude"
models = ["anthropic/claude-opus-5", "anthropic/claude-sonnet-5"]

[no_label_here]
models = ["openai/gpt-5.6-sol"]
"""

KNOWN_IDS = [
    "anthropic/claude-opus-5",
    "anthropic/claude-sonnet-5",
    "openai/gpt-5.6-sol",
]


def write(tmp_path: Path, content: str, name: str = "presets.toml") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_sections_in_file_order(tmp_path: Path) -> None:
    presets = load_presets(write(tmp_path, VALID))

    assert list(presets) == ["flagships", "anthropic", "no_label_here"]


def test_label_falls_back_to_the_section_key(tmp_path: Path) -> None:
    presets = load_presets(write(tmp_path, VALID))

    assert presets["anthropic"].label == "Anthropic — Claude"
    assert presets["no_label_here"].label == "No Label Here"


def test_duplicate_ids_collapse(tmp_path: Path) -> None:
    content = """
    [dupes]
    models = ["a/one", "a/two", "a/one"]
    """
    presets = load_presets(write(tmp_path, content))

    # Two identical columns would look like a bug in the app, not a typo.
    assert presets["dupes"].models == ("a/one", "a/two")


def test_unknown_ids_are_dropped_and_reported() -> None:
    presets = {
        "mixed": Preset(
            key="mixed",
            label="Mixed",
            models=("anthropic/claude-opus-5", "vendor/retired-last-month"),
        )
    }

    result = validate_presets(presets, KNOWN_IDS)

    assert result.presets["mixed"].models == ("anthropic/claude-opus-5",)
    assert len(result.warnings) == 1
    assert "vendor/retired-last-month" in result.warnings[0]


def test_a_preset_left_empty_is_dropped_whole() -> None:
    presets = {
        "gone": Preset(key="gone", label="Gone", models=("vendor/retired",)),
        "fine": Preset(key="fine", label="Fine", models=("openai/gpt-5.6-sol",)),
    }

    result = validate_presets(presets, KNOWN_IDS)

    assert "gone" not in result.presets
    assert "fine" in result.presets
    assert any("dropped" in warning for warning in result.warnings)


def test_validation_keeps_order_and_never_raises() -> None:
    presets = {
        "a": Preset(key="a", label="A", models=("openai/gpt-5.6-sol",)),
        "b": Preset(key="b", label="B", models=("nope/nope",)),
        "c": Preset(key="c", label="C", models=("anthropic/claude-opus-5",)),
    }

    result = validate_presets(presets, KNOWN_IDS)

    assert list(result.presets) == ["a", "c"]


def test_valid_file_produces_no_warnings(tmp_path: Path) -> None:
    result = validate_presets(load_presets(write(tmp_path, VALID)), KNOWN_IDS)

    assert result.warnings == ()
    assert len(result.presets) == 3


def test_malformed_toml_names_the_file_and_the_problem(tmp_path: Path) -> None:
    path = write(tmp_path, "[unclosed\nmodels = [", name="broken.toml")

    with pytest.raises(PresetError) as error:
        load_presets(path)

    assert "broken.toml" in str(error.value)
    assert "invalid TOML" in str(error.value)


def test_missing_file_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(PresetError, match="not found"):
        load_presets(tmp_path / "absent.toml")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('[a]\nlabel = "no models key"', "missing the required `models` key"),
        ('[a]\nmodels = "not-a-list"', "must be a list of model id strings"),
        ("[a]\nmodels = [1, 2]", "must be a list of model id strings"),
        ('[a]\nmodels = ["x/y"]\nlabel = 42', "label must be a string"),
    ],
)
def test_structural_problems_raise_with_a_useful_message(
    tmp_path: Path, content: str, expected: str
) -> None:
    with pytest.raises(PresetError, match=expected):
        load_presets(write(tmp_path, content))


CATALOG_URL = "https://openrouter.test/api/v1"


def catalog_entry(model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "name": model_id,
        "context_length": 128000,
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
    }


@respx.mock
async def test_resolve_presets_validates_against_the_live_catalog(tmp_path: Path) -> None:
    respx.get(f"{CATALOG_URL}/models").mock(
        return_value=httpx.Response(
            200,
            # The catalog knows only one of the three ids used in VALID.
            json={"data": [catalog_entry("anthropic/claude-opus-5")]},
        )
    )
    settings = Settings(openrouter_base_url=CATALOG_URL)

    async with httpx.AsyncClient() as http:
        catalog = Catalog(CatalogClient(settings, http), ttl_seconds=3600.0)
        result = await resolve_presets(write(tmp_path, VALID), catalog)

    assert list(result.presets) == ["flagships", "anthropic"]
    assert result.presets["flagships"].models == ("anthropic/claude-opus-5",)
    # `no_label_here` held only an unknown id, so it disappears entirely.
    assert any("no_label_here" in warning for warning in result.warnings)


def test_the_shipped_presets_file_is_valid() -> None:
    """Guards the real file: a typo here breaks the app for everyone."""
    presets = load_presets(SHIPPED_PRESETS)

    assert len(presets) == 10
    assert next(iter(presets)) == "flagships"
    assert presets["flagships"].models[0] == "anthropic/claude-opus-5"
    assert "google/gemini-3.1-pro-preview" in presets["google"].models
    assert all(preset.models for preset in presets.values())
    assert all("/" in model for preset in presets.values() for model in preset.models)
