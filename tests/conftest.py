import pytest


@pytest.fixture
def app_client_factory(tmp_path, monkeypatch):
    """Build TestClients with isolated data dir and test config.

    Accepts extra env vars, e.g. app_client_factory(YNAB_CLIENT_ID="x", ...)
    for OAuth-enabled apps.
    """
    from fastapi.testclient import TestClient

    def factory(**extra_env):
        monkeypatch.setenv("SECRET_KEY", "test-secret")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        for name in (
            "APP_PASSWORD",
            "YNAB_TOKEN",
            "YNAB_CLIENT_ID",
            "YNAB_CLIENT_SECRET",
            "PUBLIC_BASE_URL",
        ):
            monkeypatch.delenv(name, raising=False)
        for key, value in extra_env.items():
            monkeypatch.setenv(key, value)

        import app.config as config
        import app.routes.conversions as conversions_routes
        import app.ynab as ynab_mod

        monkeypatch.setattr(config, "_settings", None)
        monkeypatch.setattr(conversions_routes, "_rates_client", None)
        # Fresh per-conversion apply locks each test: an asyncio.Lock is bound
        # to the event loop it was created on, and every test builds a new
        # TestClient (new loop), so a cached lock would raise "bound to a
        # different event loop" if a conversion_id ever recurred.
        monkeypatch.setattr(conversions_routes, "_apply_locks", {})
        monkeypatch.setattr(ynab_mod, "_pooled_client", None)

        import app.http as app_http

        monkeypatch.setattr(app_http, "RETRY_DELAY_SECONDS", 0)

        import app.auth as auth

        auth._reset_throttle()  # login-throttle state is module-level

        from app.main import create_app

        return TestClient(create_app())

    return factory


@pytest.fixture
def app_client(app_client_factory):
    with app_client_factory() as client:
        yield client
