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
