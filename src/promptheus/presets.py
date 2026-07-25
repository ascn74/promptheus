"""Named model sets, loaded from `presets.toml` and checked against the catalog."""

from __future__ import annotations

import tomllib
from collections.abc import Collection
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from promptheus.catalog import Catalog


class PresetError(Exception):
    """The presets file is unreadable or structurally wrong.

    Raised only for problems the user must fix by editing the file. A merely
    stale model id is a warning, not an error — see `validate_presets`.
    """


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    models: tuple[str, ...]


@dataclass(frozen=True)
class PresetsResult:
    """Usable presets, plus everything the user should know about the file."""

    presets: dict[str, Preset]
    warnings: tuple[str, ...] = ()


def load_presets(path: Path) -> dict[str, Preset]:
    """Parse the presets file, preserving the order sections appear in it.

    Raises `PresetError` with the file name and the specific problem — this is
    a hand-edited file, so the message has to be enough to fix it.
    """
    try:
        raw_text = path.read_bytes()
    except FileNotFoundError as error:
        raise PresetError(f"{path}: presets file not found") from error
    except OSError as error:
        raise PresetError(f"{path}: could not read presets file: {error}") from error

    try:
        document: dict[str, Any] = tomllib.loads(raw_text.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise PresetError(f"{path}: presets file is not valid UTF-8: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise PresetError(f"{path}: invalid TOML: {error}") from error

    return {key: _build_preset(path, key, section) for key, section in document.items()}


def _build_preset(path: Path, key: str, section: Any) -> Preset:
    if not isinstance(section, dict):
        raise PresetError(f"{path}: [{key}] must be a table, got {type(section).__name__}")

    if "models" not in section:
        raise PresetError(f"{path}: [{key}] is missing the required `models` key")

    models = section["models"]
    if not isinstance(models, list) or not all(isinstance(item, str) for item in models):
        raise PresetError(f"{path}: [{key}].models must be a list of model id strings")

    label = section.get("label")
    if label is not None and not isinstance(label, str):
        raise PresetError(f"{path}: [{key}].label must be a string")

    return Preset(
        key=key,
        label=label or _default_label(key),
        # Deduplicated: the same id twice would render two identical columns,
        # which reads as a bug in the app rather than a typo in the file.
        models=tuple(dict.fromkeys(models)),
    )


def _default_label(key: str) -> str:
    return key.replace("-", " ").replace("_", " ").title()


def validate_presets(
    presets: dict[str, Preset],
    known_model_ids: Collection[str],
) -> PresetsResult:
    """Drop model ids the catalog does not know, reporting each one.

    Vendors retire models, so a stale id in a hand-edited file is expected and
    must not stop the app from starting. The rest of the preset survives; a
    preset left with nothing is dropped whole.
    """
    known = set(known_model_ids)
    kept: dict[str, Preset] = {}
    warnings: list[str] = []

    for key, preset in presets.items():
        live = tuple(model for model in preset.models if model in known)
        missing = [model for model in preset.models if model not in known]

        if missing:
            warnings.append(
                f"[{key}] skipped {len(missing)} unknown model id(s): {', '.join(missing)}"
            )

        if not live:
            warnings.append(f"[{key}] dropped: no known models left")
            continue

        kept[key] = replace(preset, models=live)

    return PresetsResult(presets=kept, warnings=tuple(warnings))


async def resolve_presets(path: Path, catalog: Catalog) -> PresetsResult:
    """Load the file and validate it against the live catalog."""
    presets = load_presets(path)
    known_ids = [model.id for model in await catalog.all()]
    return validate_presets(presets, known_ids)
