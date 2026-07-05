import pytest


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """A TestClient for the app with isolated data dir and test config."""
    monkeypatch.setenv("APP_PASSWORD", "test-password")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("YNAB_TOKEN", "test-token")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    import app.config as config
    import app.routes.conversions as conversions_routes

    monkeypatch.setattr(config, "_settings", None)
    monkeypatch.setattr(conversions_routes, "_rates_client", None)
    monkeypatch.setattr(conversions_routes, "_ynab_client", None)

    import app.http as app_http

    monkeypatch.setattr(app_http, "RETRY_DELAY_SECONDS", 0)

    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client
