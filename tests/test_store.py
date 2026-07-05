from app.store import ConversionStore


def test_add_get_delete_roundtrip(tmp_path):
    store = ConversionStore(tmp_path)
    assert store.load() == []

    conversion = store.add({"account_name": "Japan Trip", "from_currency": "JPY"})
    assert conversion["id"]
    assert store.get(conversion["id"])["account_name"] == "Japan Trip"

    # survives a fresh instance (actually persisted)
    assert ConversionStore(tmp_path).get(conversion["id"]) is not None

    store.delete(conversion["id"])
    assert store.load() == []
    assert store.get(conversion["id"]) is None


def test_update(tmp_path):
    store = ConversionStore(tmp_path)
    conversion = store.add({"account_name": "Japan Trip", "start_date": "2024-01-01"})

    updated = store.update(conversion["id"], {"start_date": "2024-06-01"})
    assert updated["start_date"] == "2024-06-01"
    assert updated["account_name"] == "Japan Trip"
    assert updated["id"] == conversion["id"]
    # persisted, not just returned
    assert ConversionStore(tmp_path).get(conversion["id"])["start_date"] == "2024-06-01"

    assert store.update("missing", {"start_date": "2024-06-01"}) is None
