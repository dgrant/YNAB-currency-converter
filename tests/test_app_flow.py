"""End-to-end HTTP flow: login -> create conversion -> preview -> apply.

YNAB and Frankfurter are mocked with respx; assertions check the exact
PATCH body sent to YNAB.
"""
import json
import re

import respx
from httpx import Response

YNAB = "https://api.ynab.com/v1"
FX = "https://api.frankfurter.dev/v1"


CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def get_csrf(client):
    """The login page (like every form page) embeds the session's CSRF token."""
    return CSRF_RE.search(client.get("/login").text).group(1)


def login(client):
    token = get_csrf(client)
    response = client.post(
        "/login",
        data={"password": "test-password", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return token


def mock_budgets(iso_code="USD"):
    """Create/edit validate to_currency against the budget's currency in YNAB."""
    return respx.get(f"{YNAB}/budgets").mock(
        return_value=Response(200, json={"data": {"budgets": [
            {"id": "b1", "name": "My Budget",
             "currency_format": {"iso_code": iso_code}},
        ]}})
    )


def test_security_headers_present(app_client):
    response = app_client.get("/login")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_login_required(app_client):
    response = app_client.get("/conversions", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_wrong_password_rejected(app_client):
    token = get_csrf(app_client)
    response = app_client.post("/login", data={"password": "nope", "csrf_token": token})
    assert response.status_code == 401


def test_login_brute_force_throttled(app_client):
    import app.auth as auth

    token = get_csrf(app_client)
    for _ in range(auth.LOCKOUT_THRESHOLD):
        response = app_client.post("/login", data={"password": "nope", "csrf_token": token})
        assert response.status_code == 401
    # locked out now — even the correct password is refused until the delay passes
    response = app_client.post(
        "/login", data={"password": "test-password", "csrf_token": token}
    )
    assert response.status_code == 429
    assert "Too many failed attempts" in response.text
    # once the lockout expires, the correct password works and resets the counter
    auth._throttle["locked_until"] = 0.0
    response = app_client.post(
        "/login", data={"password": "test-password", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert auth._throttle["failures"] == 0


def test_post_without_csrf_token_rejected(app_client):
    # no token at all
    assert app_client.post("/login", data={"password": "test-password"}).status_code == 403
    # wrong token
    get_csrf(app_client)
    response = app_client.post(
        "/login", data={"password": "test-password", "csrf_token": "forged"}
    )
    assert response.status_code == 403
    # authenticated POSTs are protected too
    token = login(app_client)
    assert app_client.post("/conversions/x/delete", data={}).status_code == 403
    assert app_client.post(
        "/conversions/x/delete", data={"csrf_token": token}, follow_redirects=False
    ).status_code == 303  # valid token: passes CSRF, delete of unknown id just redirects


@respx.mock
def test_full_conversion_flow(app_client):
    mock_budgets()
    respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [
            {"id": "t1", "date": "2024-01-05", "amount": -1817000,
             "payee_name": "Ramen", "memo": None, "deleted": False},
            {"id": "t2", "date": "2024-01-05", "amount": -5000000,
             "payee_name": "Hotel", "memo": "-5,000 JPY (FX rate: 0.0087987)",
             "deleted": False},
            {"id": "t3", "date": "2024-01-05", "amount": -3000000,
             "payee_name": "Combini", "memo": None, "deleted": False,
             "subtransactions": [{"id": "s1", "amount": -1000000},
                                 {"id": "s2", "amount": -2000000}]},
        ]}})
    )
    respx.get(f"{FX}/2023-12-29..2024-01-05").mock(
        return_value=Response(200, json={"rates": {"2024-01-05": {"USD": 0.0087987}}})
    )
    patch_route = respx.patch(f"{YNAB}/budgets/b1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [{"id": "t1"}]}})
    )

    token = login(app_client)

    response = app_client.post("/conversions", data={
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a1", "account_name": "Japan Trip",
        "from_currency": "JPY", "to_currency": "USD",
        "start_date": "2024-01-01", "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    conversion_id = response.headers["location"].rsplit("/", 1)[-1]

    preview = app_client.post(
        f"/conversions/{conversion_id}/preview", data={"csrf_token": token}
    )
    assert preview.status_code == 200
    # t1 proposed; t2 already converted (rmillan memo) so it must not appear;
    # t3 is a split and must be skipped with a note
    assert 'value="t1"' in preview.text
    assert 'value="t2"' not in preview.text
    assert 'value="t3"' not in preview.text
    assert "1 split transaction skipped" in preview.text
    assert "-1,817 JPY (FX rate: 0.0087987)" in preview.text
    # totals row for the single proposed transaction
    assert "Total (1 row)" in preview.text
    assert "-15.99" in preview.text

    applied = app_client.post(f"/conversions/{conversion_id}/apply", data={
        "selected": ["t1"],
        "amount_t1": "-15990",
        "memo_t1": "-1,817 JPY (FX rate: 0.0087987)",
        "csrf_token": token,
    })
    assert applied.status_code == 200

    assert patch_route.called
    body = json.loads(patch_route.calls[0].request.content)
    assert body == {"transactions": [
        {"id": "t1", "amount": -15990, "memo": "-1,817 JPY (FX rate: 0.0087987)"}
    ]}


@respx.mock
def test_apply_with_nothing_selected_patches_nothing(app_client):
    mock_budgets()
    patch_route = respx.patch(f"{YNAB}/budgets/b1/transactions")
    token = login(app_client)
    response = app_client.post("/conversions", data={
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a1", "account_name": "Japan Trip",
        "from_currency": "JPY", "to_currency": "USD",
        "start_date": "2024-01-01", "csrf_token": token,
    }, follow_redirects=False)
    conversion_id = response.headers["location"].rsplit("/", 1)[-1]

    applied = app_client.post(
        f"/conversions/{conversion_id}/apply", data={"csrf_token": token}
    )
    assert applied.status_code == 200
    assert not patch_route.called


@respx.mock
def test_edit_conversion(app_client):
    respx.get(f"{YNAB}/budgets").mock(
        return_value=Response(200, json={"data": {"budgets": [
            {"id": "b1", "name": "My Budget",
             "currency_format": {"iso_code": "USD"}},
        ]}})
    )
    respx.get(f"{YNAB}/budgets/b1/accounts").mock(
        return_value=Response(200, json={"data": {"accounts": [
            {"id": "a1", "name": "Japan Trip", "deleted": False, "closed": False},
            {"id": "a2", "name": "Europe Trip", "deleted": False, "closed": False},
        ]}})
    )
    respx.get(f"{FX}/currencies").mock(
        return_value=Response(200, json={"JPY": "Japanese Yen", "EUR": "Euro", "USD": "US Dollar"})
    )

    token = login(app_client)

    # the same template also serves the new-conversion form
    new_form = app_client.get("/conversions/new")
    assert new_form.status_code == 200
    assert "New conversion" in new_form.text

    response = app_client.post("/conversions", data={
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a1", "account_name": "Japan Trip",
        "from_currency": "JPY", "to_currency": "USD",
        "start_date": "2024-01-01", "csrf_token": token,
    }, follow_redirects=False)
    conversion_id = response.headers["location"].rsplit("/", 1)[-1]

    # the new form now marks a1 as taken…
    new_form = app_client.get("/conversions/new")
    assert 'new Set(["a1"])' in new_form.text

    # …but the edit form keeps the conversion's own account selectable
    form = app_client.get(f"/conversions/{conversion_id}/edit")
    assert form.status_code == 200
    assert "Edit conversion" in form.text
    assert 'value="2024-01-01"' in form.text  # start_date prefilled
    assert "new Set([])" in form.text

    response = app_client.post(f"/conversions/{conversion_id}/edit", data={
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a2", "account_name": "Europe Trip",
        "from_currency": "EUR", "to_currency": "USD",
        "start_date": "2024-03-15", "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/conversions/{conversion_id}"

    detail = app_client.get(f"/conversions/{conversion_id}")
    assert "Europe Trip" in detail.text
    assert "EUR → USD" in detail.text
    assert "2024-03-15" in detail.text

    # editing a nonexistent conversion is a 404, and GET too
    assert app_client.get("/conversions/nope/edit").status_code == 404
    assert app_client.post("/conversions/nope/edit", data={
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a1", "account_name": "Japan Trip",
        "from_currency": "JPY", "to_currency": "USD",
        "start_date": "2024-01-01", "csrf_token": token,
    }).status_code == 404


@respx.mock
def test_to_currency_must_match_budget_currency(app_client):
    # The budget is USD, so a conversion "to EUR" is a form mismatch -> 400
    mock_budgets(iso_code="USD")
    token = login(app_client)
    response = app_client.post("/conversions", data={
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a1", "account_name": "Japan Trip",
        "from_currency": "JPY", "to_currency": "EUR",
        "start_date": "2024-01-01", "csrf_token": token,
    })
    assert response.status_code == 400
    assert "uses USD" in response.text

    # an unknown budget id is rejected too
    response = app_client.post("/conversions", data={
        "budget_id": "nope", "budget_name": "Ghost",
        "account_id": "a9", "account_name": "Ghost account",
        "from_currency": "JPY", "to_currency": "USD",
        "start_date": "2024-01-01", "csrf_token": token,
    })
    assert response.status_code == 400


@respx.mock
def test_duplicate_account_rejected(app_client):
    mock_budgets()
    token = login(app_client)
    japan = {
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a1", "account_name": "Japan Trip",
        "from_currency": "JPY", "to_currency": "USD",
        "start_date": "2024-01-01", "csrf_token": token,
    }
    assert app_client.post("/conversions", data=japan, follow_redirects=False).status_code == 303

    # a second conversion for the same account is rejected
    assert app_client.post("/conversions", data=japan).status_code == 409

    # so is editing another conversion onto that account
    europe = {**japan, "account_id": "a2", "account_name": "Europe Trip", "from_currency": "EUR"}
    response = app_client.post("/conversions", data=europe, follow_redirects=False)
    europe_id = response.headers["location"].rsplit("/", 1)[-1]
    assert app_client.post(f"/conversions/{europe_id}/edit", data=japan).status_code == 409

    # editing a conversion without changing its account stays allowed
    updated = {**europe, "start_date": "2024-02-01"}
    assert app_client.post(
        f"/conversions/{europe_id}/edit", data=updated, follow_redirects=False
    ).status_code == 303
