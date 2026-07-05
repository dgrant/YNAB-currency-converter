"""Friendly error pages and retry behavior for upstream failures."""
import respx
from httpx import ConnectError, Response

from tests.test_app_flow import login, mock_budgets

YNAB = "https://api.ynab.com/v1"
FX = "https://api.frankfurter.dev/v1"


def make_conversion(client, token):
    mock_budgets()
    response = client.post("/conversions", data={
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a1", "account_name": "Japan Trip",
        "from_currency": "JPY", "to_currency": "USD",
        "start_date": "2024-01-01", "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


@respx.mock
def test_ynab_down_renders_friendly_page(app_client):
    respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        side_effect=[Response(503, text="upstream down"), Response(503, text="upstream down")]
    )
    token = login(app_client)
    conversion_id = make_conversion(app_client, token)
    response = app_client.post(
        f"/conversions/{conversion_id}/preview", data={"csrf_token": token}
    )
    assert response.status_code == 502
    assert "YNAB error" in response.text
    assert "YNAB may be down" in response.text


@respx.mock
def test_ynab_rate_limit_renders_429_page(app_client):
    respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(429, json={"error": {"detail": "Too many requests"}})
    )
    token = login(app_client)
    conversion_id = make_conversion(app_client, token)
    response = app_client.post(
        f"/conversions/{conversion_id}/preview", data={"csrf_token": token}
    )
    assert response.status_code == 429
    assert "rate limit" in response.text
    assert "200" in response.text  # explains the ~200 req/hour budget


@respx.mock
def test_transient_ynab_failure_is_retried(app_client):
    route = respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        side_effect=[
            Response(503, text="blip"),
            Response(200, json={"data": {"transactions": []}}),
        ]
    )
    token = login(app_client)
    conversion_id = make_conversion(app_client, token)
    response = app_client.post(
        f"/conversions/{conversion_id}/preview", data={"csrf_token": token}
    )
    assert response.status_code == 200
    assert "Nothing to convert" in response.text
    assert route.call_count == 2


@respx.mock
def test_connection_error_is_retried_then_friendly(app_client):
    respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        side_effect=ConnectError("boom")
    )
    token = login(app_client)
    conversion_id = make_conversion(app_client, token)
    response = app_client.post(
        f"/conversions/{conversion_id}/preview", data={"csrf_token": token}
    )
    assert response.status_code == 502
    assert "Could not reach YNAB" in response.text


@respx.mock
def test_rates_down_renders_friendly_page(app_client):
    respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [
            {"id": "t1", "date": "2024-01-05", "amount": -1817000,
             "payee_name": "Ramen", "memo": None, "deleted": False},
        ]}})
    )
    respx.get(f"{FX}/2023-12-29..2024-01-05").mock(
        side_effect=[Response(500, text="oops"), Response(500, text="oops")]
    )
    token = login(app_client)
    conversion_id = make_conversion(app_client, token)
    response = app_client.post(
        f"/conversions/{conversion_id}/preview", data={"csrf_token": token}
    )
    assert response.status_code == 502
    assert "Exchange-rate error" in response.text
    assert "Nothing was" in response.text
