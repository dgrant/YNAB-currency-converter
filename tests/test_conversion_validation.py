"""Same-currency rejection (a no-op that can only harm) and the earlier
start-date default. Reuses the mocks/helpers from the full-flow test module."""
import re
from datetime import date, timedelta

import respx
from httpx import Response
from test_app_flow import FX, YNAB, create_conversion, login, mock_budgets, mock_categories


@respx.mock
def test_create_rejects_same_currency(app_client):
    mock_budgets(iso_code="USD")
    token = login(app_client)
    resp = app_client.post(
        "/conversions",
        data={
            "budget_id": "b1", "budget_name": "My Budget",
            "account_id": "a1", "account_name": "USD Cash",
            "from_currency": "USD", "to_currency": "USD",
            "start_date": "2024-01-01", "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "nothing to convert" in resp.text


@respx.mock
def test_edit_rejects_same_currency(app_client):
    mock_budgets(iso_code="USD")
    token = login(app_client)
    cid = create_conversion(app_client, token, "a1", "Japan Trip", from_currency="JPY")
    resp = app_client.post(
        f"/conversions/{cid}/edit",
        data={
            "budget_id": "b1", "budget_name": "My Budget",
            "account_id": "a1", "account_name": "Japan Trip",
            "from_currency": "USD", "to_currency": "USD",
            "start_date": "2024-01-01", "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400


@respx.mock
def test_batch_skips_same_currency_row(app_client):
    """A batch row whose original currency equals the plan currency is dropped
    (not a 400 that fails the whole batch)."""
    respx.get(f"{YNAB}/budgets").mock(return_value=Response(200, json={"data": {"budgets": [
        {"id": "b1", "name": "My Budget", "currency_format": {"iso_code": "USD"}},
    ]}}))
    respx.get(f"{YNAB}/budgets/b1/accounts").mock(
        return_value=Response(200, json={"data": {"accounts": [
            {"id": "a1", "name": "Japan JPY", "deleted": False, "closed": False},
            {"id": "a2", "name": "USD Cash", "deleted": False, "closed": False},
        ]}})
    )
    respx.get(f"{FX}/currencies").mock(return_value=Response(200, json={
        "JPY": "Japanese Yen", "USD": "US Dollar"}))
    token = login(app_client)

    resp = app_client.post(
        "/conversions/batch",
        data={
            "create": ["a1", "a2"],
            "from_a1": "JPY", "start_a1": "2024-01-01",
            "from_a2": "USD", "start_a2": "2024-01-01",  # same as plan -> skipped
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/conversions?created=1"
    index = app_client.get("/conversions").text
    assert "Japan JPY" in index and "USD Cash" not in index


@respx.mock
def test_new_form_defaults_start_date_to_lookback(app_client):
    """The new form prefills start_date ~30 days back, not today, so a fresh
    conversion catches the pre-setup backlog."""
    mock_budgets(iso_code="USD")
    respx.get(f"{YNAB}/budgets/b1/accounts").mock(
        return_value=Response(200, json={"data": {"accounts": [
            {"id": "a1", "name": "Japan Trip", "deleted": False, "closed": False},
        ]}})
    )
    mock_categories()
    respx.get(f"{FX}/currencies").mock(
        return_value=Response(200, json={"JPY": "Japanese Yen", "USD": "US Dollar"})
    )
    login(app_client)

    expected = (date.today() - timedelta(days=30)).isoformat()
    form = app_client.get("/conversions/new")
    assert form.status_code == 200
    # Pull the start_date input's actual value out of the rendered HTML rather
    # than substring-matching template whitespace (which would silently pass if
    # the form is ever reflowed).
    m = re.search(r'id="start_date"[^>]*\bvalue="([^"]+)"', form.text)
    assert m is not None
    assert m.group(1) == expected  # 30 days back, not today
    assert m.group(1) != date.today().isoformat()
