"""Configuration from the environment, or from `.env` in the repository root."""

from pathlib import Path

from pydantic import HttpUrl, SecretStr, ValidationError, field_validator
from pydantic_core import PydanticCustomError
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
    base_url: HttpUrl
    # The web app's own header, as `name: value`. Bookings are refused without it.
    app_header: SecretStr

    @field_validator("cookie")
    @classmethod
    def _cookie_has_name_and_value(cls, cookie: SecretStr) -> SecretStr:
        # `.env.example` ships with `Authorization=` filled in: a name without a value is missing.
        name, separator, value = cookie.get_secret_value().partition("=")
        if separator and not value.strip():
            raise PydanticCustomError("missing", "no cookie value")
        if not separator or not name.strip():
            raise PydanticCustomError("cookie_format", "expected name=value")
        return cookie

    @field_validator("base_url")
    @classmethod
    def _base_url_is_an_https_host(cls, base_url: HttpUrl) -> HttpUrl:
        # The client puts `/api/v2/...` after it: a path would be doubled or prefixed, and
        # http would send the cookie in the clear.
        if base_url.scheme != "https" or base_url.path not in (None, "", "/"):
            raise PydanticCustomError("base_url_format", "expected https://<host>")
        return base_url

    @field_validator("app_header")
    @classmethod
    def _app_header_has_name_and_value(cls, app_header: SecretStr) -> SecretStr:
        name, separator, value = app_header.get_secret_value().partition(":")
        if not separator or not name.strip() or not value.strip():
            raise PydanticCustomError("app_header_format", "expected name: value")
        return app_header

    def app_header_pair(self) -> tuple[str, str]:
        """The app header's name and value, split on the first colon."""
        name, _, value = self.app_header.get_secret_value().partition(":")
        return name.strip(), value.strip()


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
