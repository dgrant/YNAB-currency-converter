"""YNABClient.get_categories filtering: deleted/hidden categories, hidden and
internal-master groups, and empty groups are all dropped from the picker."""
import respx
from httpx import Response

from app.ynab import YNABClient

YNAB = "https://api.ynab.com/v1"

_GROUPS = [
    {"id": "cg1", "name": "Everyday", "deleted": False, "hidden": False, "categories": [
        {"id": "cat1", "name": "Groceries", "deleted": False, "hidden": False},
        {"id": "cat_del", "name": "Gone", "deleted": True, "hidden": False},
        {"id": "cat_hid", "name": "Hidden", "deleted": False, "hidden": True},
    ]},
    {"id": "cg_hidden", "name": "Hidden Group", "deleted": False, "hidden": True, "categories": [
        {"id": "catx", "name": "X", "deleted": False, "hidden": False},
    ]},
    {"id": "cg_master", "name": "Internal Master Category", "deleted": False,
     "hidden": False, "categories": [
        {"id": "rta", "name": "Inflow: Ready to Assign", "deleted": False, "hidden": False},
    ]},
    {"id": "cg_empty", "name": "All Gone", "deleted": False, "hidden": False, "categories": [
        {"id": "z", "name": "Z", "deleted": True, "hidden": False},
    ]},
]


@respx.mock
def test_get_categories_filters(monkeypatch):
    import app.ynab as ynab_mod
    monkeypatch.setattr(ynab_mod, "_pooled_client", None)
    respx.get(f"{YNAB}/budgets/b1/categories").mock(
        return_value=Response(200, json={"data": {"category_groups": _GROUPS}})
    )
    client = YNABClient("tok", YNAB)

    groups = client.get_categories("b1")
    # only the Everyday group survives (hidden group, internal-master, and the
    # all-deleted group are dropped); within it only the live, visible category
    assert [g["name"] for g in groups] == ["Everyday"]
    assert [c["id"] for c in groups[0]["categories"]] == ["cat1"]
    # the flat id set used at apply time matches
    assert client.category_ids("b1") == {"cat1"}
