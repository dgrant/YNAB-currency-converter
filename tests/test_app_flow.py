"""End-to-end HTTP flow: login -> create conversion -> preview -> apply.

YNAB and Frankfurter are mocked with respx; assertions check the exact
PATCH body sent to YNAB.
"""
import json

import respx
from httpx import Response

YNAB = "https://api.ynab.com/v1"
FX = "https://api.frankfurter.dev/v1"


def login(client):
    response = client.post("/login", data={"password": "test-password"}, follow_redirects=False)
    assert response.status_code == 303


def test_login_required(app_client):
    response = app_client.get("/conversions", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_wrong_password_rejected(app_client):
    response = app_client.post("/login", data={"password": "nope"})
    assert response.status_code == 401


@respx.mock
def test_full_conversion_flow(app_client):
    respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [
            {"id": "t1", "date": "2024-01-05", "amount": -1817000,
             "payee_name": "Ramen", "memo": None, "deleted": False},
            {"id": "t2", "date": "2024-01-05", "amount": -5000000,
             "payee_name": "Hotel", "memo": "-5,000 JPY (FX rate: 0.0087987)",
             "deleted": False},
        ]}})
    )
    respx.get(f"{FX}/2023-12-29..2024-01-05").mock(
        return_value=Response(200, json={"rates": {"2024-01-05": {"USD": 0.0087987}}})
    )
    patch_route = respx.patch(f"{YNAB}/budgets/b1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [{"id": "t1"}]}})
    )

    login(app_client)

    response = app_client.post("/conversions", data={
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a1", "account_name": "Japan Trip",
        "from_currency": "JPY", "to_currency": "USD",
        "start_date": "2024-01-01",
    }, follow_redirects=False)
    assert response.status_code == 303
    conversion_id = response.headers["location"].rsplit("/", 1)[-1]

    preview = app_client.post(f"/conversions/{conversion_id}/preview")
    assert preview.status_code == 200
    # t1 proposed; t2 already converted (rmillan memo) so it must not appear
    assert 'value="t1"' in preview.text
    assert 'value="t2"' not in preview.text
    assert "-1,817 JPY (FX rate: 0.0087987)" in preview.text

    applied = app_client.post(f"/conversions/{conversion_id}/apply", data={
        "selected": ["t1"],
        "amount_t1": "-15990",
        "memo_t1": "-1,817 JPY (FX rate: 0.0087987)",
    })
    assert applied.status_code == 200

    assert patch_route.called
    body = json.loads(patch_route.calls[0].request.content)
    assert body == {"transactions": [
        {"id": "t1", "amount": -15990, "memo": "-1,817 JPY (FX rate: 0.0087987)"}
    ]}


@respx.mock
def test_apply_with_nothing_selected_patches_nothing(app_client):
    patch_route = respx.patch(f"{YNAB}/budgets/b1/transactions")
    login(app_client)
    response = app_client.post("/conversions", data={
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a1", "account_name": "Japan Trip",
        "from_currency": "JPY", "to_currency": "USD",
        "start_date": "2024-01-01",
    }, follow_redirects=False)
    conversion_id = response.headers["location"].rsplit("/", 1)[-1]

    applied = app_client.post(f"/conversions/{conversion_id}/apply", data={})
    assert applied.status_code == 200
    assert not patch_route.called
