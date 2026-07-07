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


def test_mark_synced(tmp_path):
    user = make_user(tmp_path)
    store = ConversionStore(tmp_path)
    conversion = store.add(user.id, CONVERSION)
    # a fresh conversion has never been synced
    assert store.get(user.id, conversion["id"])["last_synced"] is None

    store.mark_synced(user.id, conversion["id"], "2026-07-06")
    assert store.get(user.id, conversion["id"])["last_synced"] == "2026-07-06"
    # updating editable fields leaves last_synced untouched
    store.update(user.id, conversion["id"], {"start_date": "2024-06-01"})
    assert store.get(user.id, conversion["id"])["last_synced"] == "2026-07-06"


def test_mark_synced_is_scoped(tmp_path):
    alice = make_user(tmp_path, "alice@example.com")
    bob = make_user(tmp_path, "bob@example.com")
    store = ConversionStore(tmp_path)
    conversion = store.add(alice.id, CONVERSION)
    store.mark_synced(bob.id, conversion["id"], "2026-07-06")  # scoped no-op
    assert store.get(alice.id, conversion["id"])["last_synced"] is None


def test_delete_many(tmp_path):
    user = make_user(tmp_path)
    store = ConversionStore(tmp_path)
    keep = store.add(user.id, CONVERSION)
    gone1 = store.add(user.id, {**CONVERSION, "account_id": "a2", "account_name": "Trip 2"})
    gone2 = store.add(user.id, {**CONVERSION, "account_id": "a3", "account_name": "Trip 3"})

    store.delete_many(user.id, [gone1["id"], gone2["id"]])
    assert [c["id"] for c in store.load(user.id)] == [keep["id"]]

    # a no-op empty list doesn't error
    store.delete_many(user.id, [])
    assert [c["id"] for c in store.load(user.id)] == [keep["id"]]


def test_delete_many_is_scoped_per_user(tmp_path):
    alice = make_user(tmp_path, "alice@example.com")
    bob = make_user(tmp_path, "bob@example.com")
    store = ConversionStore(tmp_path)
    conversion = store.add(alice.id, CONVERSION)

    store.delete_many(bob.id, [conversion["id"]])  # scoped no-op
    assert store.get(alice.id, conversion["id"]) is not None


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
