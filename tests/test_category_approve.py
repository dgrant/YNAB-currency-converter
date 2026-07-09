"""Bulk category + approve: the apply payload carries category_id/approved
exactly where it should, and the apply-time re-check drops a category that YNAB
would reject (transfers, deleted category) so the whole batch still lands."""
import json

import respx
from httpx import Response

from tests.test_app_flow import FX, YNAB, login, mock_budgets, mock_categories

# One transaction on 2024-01-05; the rates window matches the full-flow test.
RATES_URL = f"{FX}/2023-12-29..2024-01-05"


def _mock_transactions(transactions):
    return respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": transactions}})
    )


def _mock_rates():
    respx.get(RATES_URL).mock(
        return_value=Response(200, json={"rates": {"2024-01-05": {"USD": 0.0087987}}})
    )


def _mock_patch():
    return respx.patch(f"{YNAB}/budgets/b1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [{"id": "t1"}]}})
    )


def _create(client, token, category_id="cat2", approve=True):
    """Create a conversion with a default category + approve flag (create calls
    get_categories to validate, so mock_budgets + mock_categories must be set)."""
    data = {
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a1", "account_name": "Japan Trip",
        "from_currency": "JPY", "to_currency": "USD",
        "start_date": "2024-01-01", "default_category_id": category_id,
        "csrf_token": token,
    }
    if approve:
        data["approve_on_apply"] = "on"
    resp = client.post("/conversions", data=data, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return resp.headers["location"].rsplit("/", 1)[-1]


def _apply(client, token, conversion_id, action="convert", approve=True):
    data = {
        "selected": ["t1"], "action_t1": action, "original_t1": "-1817000",
        "amount_t1": "-15990", "memo_t1": "-1,817 JPY (FX rate: 0.0087987)",
        "already_memo_t1": "x", "skip_memo_t1": "(skipped)", "csrf_token": token,
    }
    if approve:
        data["approve"] = "on"
    return client.post(f"/conversions/{conversion_id}/apply", data=data)


@respx.mock
def test_convert_with_category_and_approve(app_client):
    mock_budgets()
    mock_categories()
    _mock_transactions([
        {"id": "t1", "date": "2024-01-05", "amount": -1817000,
         "payee_name": "Ramen", "memo": None, "deleted": False},
    ])
    _mock_rates()
    patch_route = _mock_patch()
    token = login(app_client)
    cid = _create(app_client, token)
    resp = _apply(app_client, token, cid, action="convert", approve=True)

    body = json.loads(patch_route.calls[0].request.content)
    assert body == {"transactions": [
        {"id": "t1", "amount": -15990, "memo": "-1,817 JPY (FX rate: 0.0087987)",
         "category_id": "cat2", "approved": True},
    ]}
    # the detail flash confirms both, so the user doesn't re-open YNAB
    assert "Categorized 1 to 2026 Japan Vacation" in resp.text
    assert "Approved 1 in YNAB" in resp.text


@respx.mock
def test_approve_off_omits_approved(app_client):
    """Approve unchecked must OMIT the field, never send approved:false (which
    would un-approve an already-approved pile on a re-run)."""
    mock_budgets()
    mock_categories()
    _mock_transactions([
        {"id": "t1", "date": "2024-01-05", "amount": -1817000,
         "payee_name": "Ramen", "memo": None, "deleted": False},
    ])
    _mock_rates()
    patch_route = _mock_patch()
    token = login(app_client)
    cid = _create(app_client, token, approve=False)
    _apply(app_client, token, cid, action="convert", approve=False)

    row = json.loads(patch_route.calls[0].request.content)["transactions"][0]
    assert "approved" not in row
    assert row["category_id"] == "cat2"


@respx.mock
def test_convert_nocat_drops_category_but_still_approves(app_client):
    """The per-row opt-out (4th Action option) converts without a category."""
    mock_budgets()
    mock_categories()
    _mock_transactions([
        {"id": "t1", "date": "2024-01-05", "amount": -1817000,
         "payee_name": "Shoes", "memo": None, "deleted": False},
    ])
    _mock_rates()
    patch_route = _mock_patch()
    token = login(app_client)
    cid = _create(app_client, token)
    _apply(app_client, token, cid, action="convert_nocat", approve=True)

    row = json.loads(patch_route.calls[0].request.content)["transactions"][0]
    assert "category_id" not in row
    assert row["approved"] is True
    assert row["amount"] == -15990  # still converted


@respx.mock
def test_transfer_row_keeps_approve_but_drops_category(app_client):
    """A transfer can't take a category in YNAB; the apply-time re-check drops
    it (keyed on transfer_account_id) so the bulk PATCH isn't rejected wholesale."""
    mock_budgets()
    mock_categories()
    _mock_transactions([
        {"id": "t1", "date": "2024-01-05", "amount": -1817000,
         "payee_name": "Wire", "memo": None, "deleted": False,
         "transfer_account_id": "acct-x"},
    ])
    _mock_rates()
    patch_route = _mock_patch()
    token = login(app_client)
    cid = _create(app_client, token)
    resp = _apply(app_client, token, cid, action="convert", approve=True)

    row = json.loads(patch_route.calls[0].request.content)["transactions"][0]
    assert "category_id" not in row  # dropped: transfers reject categories
    assert row["approved"] is True
    assert "1 left uncategorized" in resp.text


@respx.mock
def test_create_rejects_category_not_in_budget(app_client):
    """A tampered/wrong-budget default category is a 400 at save time, so it can
    never be stored where it would fail every later apply."""
    mock_budgets()
    mock_categories()
    token = login(app_client)
    resp = app_client.post("/conversions", data={
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a1", "account_name": "Japan Trip",
        "from_currency": "JPY", "to_currency": "USD", "start_date": "2024-01-01",
        "default_category_id": "not-a-real-category", "csrf_token": token,
    }, follow_redirects=False)
    assert resp.status_code == 400


@respx.mock
def test_deleted_default_category_is_dropped_not_batch_failed(app_client):
    """If the stored default was archived/deleted in YNAB since it was set, the
    apply-time validation drops just that category (convert+approve still land)
    rather than letting YNAB reject the entire batch."""
    mock_budgets()
    with_cat2 = {"data": {"category_groups": [
        {"id": "cg1", "name": "Everyday", "deleted": False, "hidden": False, "categories": [
            {"id": "cat2", "name": "2026 Japan Vacation", "deleted": False, "hidden": False},
        ]},
    ]}}
    without_cat2 = {"data": {"category_groups": [
        {"id": "cg1", "name": "Everyday", "deleted": False, "hidden": False, "categories": [
            {"id": "cat1", "name": "Groceries", "deleted": False, "hidden": False},
        ]},
    ]}}
    # create validates against cat2 (present); by apply time it's gone.
    respx.get(f"{YNAB}/budgets/b1/categories").mock(
        side_effect=[Response(200, json=with_cat2), Response(200, json=without_cat2)]
    )
    _mock_transactions([
        {"id": "t1", "date": "2024-01-05", "amount": -1817000,
         "payee_name": "Ramen", "memo": None, "deleted": False},
    ])
    _mock_rates()
    patch_route = _mock_patch()
    token = login(app_client)
    cid = _create(app_client, token)
    _apply(app_client, token, cid, action="convert", approve=True)

    row = json.loads(patch_route.calls[0].request.content)["transactions"][0]
    assert "category_id" not in row  # cat2 no longer valid → dropped
    assert row["approved"] is True
