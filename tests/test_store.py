import pytest

from app import db
from app.store import ConversionStore, DuplicateAccountError
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


def test_delete_many_with_single_id(tmp_path):
    """The '?' * n placeholder-building idiom degenerates to a single '?'
    (no separator) for n=1 — worth locking down explicitly."""
    user = make_user(tmp_path)
    store = ConversionStore(tmp_path)
    keep = store.add(user.id, CONVERSION)
    gone = store.add(user.id, {**CONVERSION, "account_id": "a2", "account_name": "Trip 2"})

    store.delete_many(user.id, [gone["id"]])
    assert [c["id"] for c in store.load(user.id)] == [keep["id"]]


def test_delete_many_at_cap_size(tmp_path):
    """Exercises delete_many at the same size as the route's _MAX_BULK_DELETE
    cap, to catch a placeholder-count mismatch that only shows up at scale."""
    from app.routes.conversions import _MAX_BULK_DELETE

    user = make_user(tmp_path)
    store = ConversionStore(tmp_path)
    keep = store.add(user.id, CONVERSION)
    ids = [
        store.add(
            user.id, {**CONVERSION, "account_id": f"a{i + 2}", "account_name": f"Trip {i}"}
        )["id"]
        for i in range(_MAX_BULK_DELETE)
    ]

    store.delete_many(user.id, ids)
    assert [c["id"] for c in store.load(user.id)] == [keep["id"]]


def test_delete_many_is_scoped_per_user(tmp_path):
    alice = make_user(tmp_path, "alice@example.com")
    bob = make_user(tmp_path, "bob@example.com")
    store = ConversionStore(tmp_path)
    conversion = store.add(alice.id, CONVERSION)

    store.delete_many(bob.id, [conversion["id"]])  # scoped no-op
    assert store.get(alice.id, conversion["id"]) is not None


def test_add_rejects_duplicate_account_at_db_level(tmp_path):
    """The DB-level backstop behind routes/conversions.py's own pre-check
    (_reject_duplicate_account) — the unique index closes the race a plain
    check-then-insert can't."""
    user = make_user(tmp_path)
    store = ConversionStore(tmp_path)
    store.add(user.id, CONVERSION)

    with pytest.raises(DuplicateAccountError) as exc_info:
        store.add(user.id, {**CONVERSION, "account_name": "Second Trip"})
    assert exc_info.value.account_id == "a1"

    # a different account, or the same account for a different user, is fine
    store.add(user.id, {**CONVERSION, "account_id": "a2", "account_name": "Other Trip"})
    other_user = make_user(tmp_path, "other@example.com")
    store.add(other_user.id, CONVERSION)


def test_update_rejects_duplicate_account_at_db_level(tmp_path):
    user = make_user(tmp_path)
    store = ConversionStore(tmp_path)
    store.add(user.id, CONVERSION)
    other = store.add(user.id, {**CONVERSION, "account_id": "a2", "account_name": "Other Trip"})

    with pytest.raises(DuplicateAccountError):
        store.update(user.id, other["id"], {"account_id": "a1"})


def test_add_many_falls_back_and_skips_a_db_level_collision(tmp_path):
    """If the fast batched insert hits a rare race collision (another request
    inserted one of these accounts between the caller's own check and this
    call), add_many falls back to inserting one at a time so the collision
    doesn't lose the rest of a large batch."""
    user = make_user(tmp_path)
    store = ConversionStore(tmp_path)
    store.add(user.id, CONVERSION)  # a1 already exists

    inserted = store.add_many(user.id, [
        {**CONVERSION, "account_id": "a2", "account_name": "Trip 2"},
        {**CONVERSION, "account_id": "a1", "account_name": "Colliding Trip"},  # skipped
        {**CONVERSION, "account_id": "a3", "account_name": "Trip 3"},
    ])
    assert {c["account_id"] for c in inserted} == {"a2", "a3"}
    assert {c["account_id"] for c in store.load(user.id)} == {"a1", "a2", "a3"}


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
