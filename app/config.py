import os
from pathlib import Path


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


class Settings:
    def __init__(self) -> None:
        self.app_password = _require("APP_PASSWORD")
        self.secret_key = _require("SECRET_KEY")
        self.ynab_token = os.environ.get("YNAB_TOKEN", "")
        self.data_dir = Path(os.environ.get("DATA_DIR", "data"))
        self.ynab_api_base = os.environ.get("YNAB_API_BASE", "https://api.ynab.com/v1")
        self.frankfurter_api_base = os.environ.get(
            "FRANKFURTER_API_BASE", "https://api.frankfurter.dev/v1"
        )
        # Git SHA baked into the image at build time (Dockerfile ARG GIT_SHA);
        # "dev" when running outside the built image.
        self.app_version = os.environ.get("APP_VERSION", "dev")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
