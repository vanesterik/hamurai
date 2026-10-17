"""Configuration from the environment, or from `.env` in the repository root."""

from pathlib import Path

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]

ENV_PREFIX = "HAMURAI_"


class ConfigError(Exception):
    """A required variable is missing or invalid."""


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, env_ignore_empty=True, extra="ignore")

    cookie: SecretStr
    administration_id: int
    journal_id: int


def load_config(env_file: Path | None = ROOT / ".env") -> Config:
    """Read the configuration. Raises `ConfigError` naming each bad variable, never its value."""
    try:
        return Config(_env_file=env_file)
    except ValidationError as exc:
        # Built from the field names only: pydantic's own message can quote the input values.
        problems = sorted(
            {
                f"{ENV_PREFIX}{str(error['loc'][0]).upper()} is "
                + ("missing" if error["type"] == "missing" else "invalid")
                for error in exc.errors()
            }
        )
        raise ConfigError("; ".join(problems)) from None
