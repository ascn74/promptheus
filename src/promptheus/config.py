"""Application settings, read from the environment and `.env`."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration for the whole application.

    The API key is a `SecretStr` so it cannot be leaked by an accidental repr
    or a log line that dumps the settings object.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Optional on purpose: the model catalog is a public endpoint, so the app
    # can start and browse models before a key is configured. Anything that
    # actually runs a completion checks for it and fails with a clear message.
    openrouter_api_key: SecretStr | None = None

    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_app_url: str = "http://localhost:8000"
    openrouter_app_title: str = "Promptheus"

    catalog_ttl_seconds: float = 3600.0

    # Reasoning models can think for several minutes before the first token.
    request_timeout_seconds: float = 600.0

    # Named model sets, edited by hand. Relative to the working directory.
    presets_path: Path = Path("presets.toml")

    # A large document multiplied by every selected model is a surprising bill.
    max_attachment_chars: int = 500_000

    # How long a pending run stays in memory between the upload request and the
    # stream request. Process memory with a deadline, not storage.
    run_ttl_seconds: float = 3600.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings.

    Cached so every caller sees the same instance. Tests that need different
    values call `get_settings.cache_clear()` after changing the environment.
    """
    return Settings()
