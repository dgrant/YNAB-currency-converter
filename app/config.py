import os
from pathlib import Path


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _read_version() -> str:
    version_path = Path(__file__).resolve().parent.parent / "VERSION"
    try:
        return version_path.read_text().strip()
    except OSError:
        return "dev"


class Settings:
    def __init__(self) -> None:
        self.secret_key = _require("SECRET_KEY")
        # Legacy single-user leftover (the v1 login password), consumed only
        # by `python -m app.import_legacy`. The old YNAB_TOKEN env var is no
        # longer read anywhere — the app is OAuth-only, so an imported user
        # reconnects via OAuth instead.
        self.app_password = os.environ.get("APP_PASSWORD", "")
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
        # Release version: the root VERSION file (gstack four-part
        # MAJOR.MINOR.PATCH.MICRO), copied into the image alongside app/
        # (Dockerfile) and present at the repo root for local/dev runs.
        # APP_VERSION overrides it (tests, unusual deployments).
        self.app_version = os.environ.get("APP_VERSION") or _read_version()
        # Build identifier: the git SHA baked into the image at build time
        # (Dockerfile ARG GIT_SHA / BUILD_ID). Distinct from app_version —
        # VERSION only bumps on a release, so it can't be used for
        # cache-busting or exact-commit deploy verification, both of which
        # need something unique per commit. "dev" outside the built image.
        self.build_id = os.environ.get("BUILD_ID", "dev")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
