import os
from pathlib import Path


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


class Settings:
    def __init__(self) -> None:
        self.secret_key = _require("SECRET_KEY")
        # Legacy single-user leftovers, consumed only by `python -m
        # app.import_legacy` (they were the v1 login password and YNAB token).
        self.app_password = os.environ.get("APP_PASSWORD", "")
        self.ynab_token = os.environ.get("YNAB_TOKEN", "")
        self.data_dir = Path(os.environ.get("DATA_DIR", "data"))
        self.ynab_api_base = os.environ.get("YNAB_API_BASE", "https://api.ynab.com/v1")
        self.frankfurter_api_base = os.environ.get(
            "FRANKFURTER_API_BASE", "https://api.frankfurter.dev/v1"
        )
        # YNAB OAuth application credentials (register at YNAB Developer
        # Settings). Required for users to connect their YNAB account.
        self.ynab_client_id = os.environ.get("YNAB_CLIENT_ID", "")
        self.ynab_client_secret = os.environ.get("YNAB_CLIENT_SECRET", "")
        self.ynab_oauth_base = os.environ.get("YNAB_OAUTH_BASE", "https://app.ynab.com")
        # Public origin (e.g. https://fxforynab.davidgrant.ca) used to build the
        # OAuth redirect URI behind the reverse proxy; derived from the
        # request when unset.
        self.public_base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        # Set true on the HTTPS deployment so the session cookie gets Secure and
        # an HSTS header is sent; keep false for local http dev.
        self.session_https_only = os.environ.get("SESSION_HTTPS_ONLY", "").lower() in (
            "1",
            "true",
            "yes",
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
