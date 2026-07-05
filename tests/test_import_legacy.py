"""Migration of the v1 single-user layout (JSON + env vars) into SQLite."""
import json

import pytest

from app.connections import ConnectionStore
from app.store import ConversionStore
from app.users import UserStore, verify_password

LEGACY_CONVERSION = {
    "id": "abc12345",
    "budget_id": "b1",
    "budget_name": "My Budget",
    "account_id": "a1",
    "account_name": "Japan Trip",
    "from_currency": "JPY",
    "to_currency": "USD",
    "start_date": "2024-01-01",
}


@pytest.fixture
def legacy_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_PASSWORD", "old-password")
    monkeypatch.setenv("YNAB_TOKEN", "old-ynab-token")

    import app.config as config

    monkeypatch.setattr(config, "_settings", None)
    (tmp_path / "conversions.json").write_text(json.dumps([LEGACY_CONVERSION]))
    return tmp_path


def test_import_legacy_creates_user_token_and_conversions(legacy_env):
    from app.import_legacy import import_legacy

    message = import_legacy("david@example.com")
    assert "imported 1 conversion" in message

    user = UserStore(legacy_env).get_by_email("david@example.com")
    assert user is not None
    assert verify_password("old-password", user.password_hash)

    connection = ConnectionStore(legacy_env).get(user.id)
    assert connection.kind == "pat"
    assert connection.access_token == "old-ynab-token"

    conversions = ConversionStore(legacy_env).load(user.id)
    assert len(conversions) == 1
    assert conversions[0]["id"] == "abc12345"  # ids preserved so URLs keep working
    assert conversions[0]["account_name"] == "Japan Trip"

    # the JSON file is renamed so it can't be imported twice
    assert not (legacy_env / "conversions.json").exists()
    assert (legacy_env / "conversions.json.imported").exists()

    # re-running refuses to touch the existing user
    with pytest.raises(SystemExit):
        import_legacy("david@example.com")


def test_import_legacy_requires_app_password(legacy_env, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "")

    import app.config as config

    monkeypatch.setattr(config, "_settings", None)
    from app.import_legacy import import_legacy

    with pytest.raises(SystemExit):
        import_legacy("david@example.com")
