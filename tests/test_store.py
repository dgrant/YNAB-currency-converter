from app import db
from app.store import ConversionStore
from app.users import UserStore

CONVERSION = {
    "budget_id": "b1",
    "budget_name": "My Budget",
    "account_id": "a1",
    "account_name": "Japan Trip",
    "from_currency": "JPY",
    "to_currency": "USD",
    "start_date": "2024-01-01",
}


def make_user(tmp_path, email="u@example.com"):
    db.init(tmp_path)
    return UserStore(tmp_path).create(email, "password123")


def test_add_get_delete_roundtrip(tmp_path):
    user = make_user(tmp_path)
    store = ConversionStore(tmp_path)
    assert store.load(user.id) == []

    conversion = store.add(user.id, CONVERSION)
    assert conversion["id"]
    assert store.get(user.id, conversion["id"])["account_name"] == "Japan Trip"

    # survives a fresh instance (actually persisted)
    assert ConversionStore(tmp_path).get(user.id, conversion["id"]) is not None

    store.delete(user.id, conversion["id"])
    assert store.load(user.id) == []
    assert store.get(user.id, conversion["id"]) is None


def test_update(tmp_path):
    user = make_user(tmp_path)
    store = ConversionStore(tmp_path)
    conversion = store.add(user.id, CONVERSION)

    updated = store.update(user.id, conversion["id"], {"start_date": "2024-06-01"})
    assert updated["start_date"] == "2024-06-01"
    assert updated["account_name"] == "Japan Trip"
    assert updated["id"] == conversion["id"]
    # persisted, not just returned
    assert ConversionStore(tmp_path).get(user.id, conversion["id"])["start_date"] == "2024-06-01"

    assert store.update(user.id, "missing", {"start_date": "2024-06-01"}) is None


def test_scoping_between_users(tmp_path):
    alice = make_user(tmp_path, "alice@example.com")
    bob = make_user(tmp_path, "bob@example.com")
    store = ConversionStore(tmp_path)

    conversion = store.add(alice.id, CONVERSION)
    assert store.load(bob.id) == []
    assert store.get(bob.id, conversion["id"]) is None
    assert store.update(bob.id, conversion["id"], {"start_date": "2024-06-01"}) is None

    store.delete(bob.id, conversion["id"])  # scoped no-op
    assert store.get(alice.id, conversion["id"]) is not None
