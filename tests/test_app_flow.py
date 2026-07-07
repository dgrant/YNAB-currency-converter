"""End-to-end HTTP flow: sign up -> connect YNAB -> create conversion ->
preview -> apply.

YNAB and Frankfurter are mocked with respx; assertions check the exact
PATCH body sent to YNAB.
"""
import json
import re
import time

import respx
from httpx import Response

YNAB = "https://api.ynab.com/v1"
FX = "https://api.frankfurter.dev/v1"

EMAIL = "user@example.com"
PASSWORD = "test-password"

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def get_csrf(client):
    """The login page (like every form page) embeds the session's CSRF token."""
    return CSRF_RE.search(client.get("/login").text).group(1)


def signup(client, email=EMAIL, password=PASSWORD):
    token = get_csrf(client)
    response = client.post(
        "/signup",
        data={
            "email": email,
            "password": password,
            "password_confirm": password,
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return token


def connect_ynab(client, csrf_token=None, token="test-token", email=EMAIL):
    """Give the logged-in user a working YNAB OAuth connection (test seam).

    OAuth is the only connection type, and driving the real callback needs a
    configured OAuth app + a mocked token exchange; for setup we seed the
    connection directly in the store with a fresh (non-expiring) access token.
    """
    from app.config import get_settings
    from app.connections import ConnectionStore
    from app.users import UserStore, normalize_email

    data_dir = get_settings().data_dir
    user = UserStore(data_dir).get_by_email(normalize_email(email))
    ConnectionStore(data_dir).set_oauth(
        user.id,
        access_token=token,
        refresh_token="test-refresh",
        expires_at=time.time() + 3600,
    )
    return csrf_token


def login(client, email=EMAIL, password=PASSWORD):
    """Fresh user ready to use the app: signed up and connected to YNAB."""
    token = signup(client, email, password)
    return connect_ynab(client, token, email=email)


def mock_budgets(iso_code="USD"):
    """Create/edit validate to_currency against the budget's currency in YNAB."""
    return respx.get(f"{YNAB}/budgets").mock(
        return_value=Response(200, json={"data": {"budgets": [
            {"id": "b1", "name": "My Budget",
             "currency_format": {"iso_code": iso_code}},
        ]}})
    )


def mock_two_budgets():
    return respx.get(f"{YNAB}/budgets").mock(
        return_value=Response(200, json={"data": {"budgets": [
            {"id": "b1", "name": "My Budget", "currency_format": {"iso_code": "USD"}},
            {"id": "b2", "name": "Other Plan", "currency_format": {"iso_code": "USD"}},
        ]}})
    )


def create_conversion(client, token, account_id, account_name,
                      budget_id="b1", budget_name="My Budget", from_currency="JPY"):
    response = client.post("/conversions", data={
        "budget_id": budget_id, "budget_name": budget_name,
        "account_id": account_id, "account_name": account_name,
        "from_currency": from_currency, "to_currency": "USD",
        "start_date": "2024-01-01", "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


@respx.mock
def test_index_sorting(app_client):
    mock_budgets()
    token = login(app_client)
    create_conversion(app_client, token, "a1", "Zebra")
    create_conversion(app_client, token, "a2", "Alpha")

    # default order is insertion order (Zebra was created first)
    default = app_client.get("/conversions").text
    assert default.index("Zebra") < default.index("Alpha")

    # ascending by account name flips them and marks the active column
    asc = app_client.get("/conversions?sort=account&order=asc").text
    assert asc.index("Alpha") < asc.index("Zebra")
    assert "▲" in asc
    # the header link now offers the opposite direction
    assert "sort=account&order=desc" in asc

    desc = app_client.get("/conversions?sort=account&order=desc").text
    assert desc.index("Zebra") < desc.index("Alpha")
    assert "▼" in desc

    # an unknown sort key is ignored, not a 500
    assert app_client.get("/conversions?sort=bogus").status_code == 200


@respx.mock
def test_plan_column_collapses_to_single_plan(app_client):
    mock_two_budgets()
    token = login(app_client)
    create_conversion(app_client, token, "a1", "Japan Trip")

    # one plan → the Plan column is hidden and a note names it instead
    single = app_client.get("/conversions").text
    assert "sort=plan" not in single
    assert "All conversions are in" in single

    # a second conversion in a different plan brings the column back
    create_conversion(app_client, token, "a2", "Europe Trip",
                      budget_id="b2", budget_name="Other Plan")
    multi = app_client.get("/conversions").text
    assert "sort=plan" in multi
    assert "Other Plan" in multi
    assert "All conversions are in" not in multi


@respx.mock
def test_bulk_delete(app_client):
    mock_budgets()
    token = login(app_client)
    japan = create_conversion(app_client, token, "a1", "Japan Trip")
    europe = create_conversion(app_client, token, "a2", "Europe Trip")
    create_conversion(app_client, token, "a3", "Asia Trip")

    # missing CSRF is rejected before anything is deleted
    assert app_client.post(
        "/conversions/bulk-delete", data={"ids": [japan]}
    ).status_code == 403

    response = app_client.post("/conversions/bulk-delete", data={
        "ids": [japan, europe], "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303

    remaining = app_client.get("/conversions").text
    assert "Japan Trip" not in remaining
    assert "Europe Trip" not in remaining
    assert "Asia Trip" in remaining
    # a bulk-delete with no ids is a harmless no-op
    assert app_client.post(
        "/conversions/bulk-delete", data={"csrf_token": token}, follow_redirects=False
    ).status_code == 303
    assert "Asia Trip" in app_client.get("/conversions").text


@respx.mock
def test_bulk_delete_is_scoped_per_user(app_client):
    """Mirrors test_users_cannot_see_each_others_conversions but for the bulk
    route: mixing another user's conversion id into the request must not
    delete it, even though the ids are attacker-controlled form values."""
    from fastapi.testclient import TestClient

    mock_budgets()
    alice_token = login(app_client, email="alice@example.com")
    alice_conversion = create_conversion(app_client, alice_token, "a1", "Japan Trip")

    with TestClient(app_client.app) as bob:
        bob_token = login(bob, email="bob@example.com")
        bob_conversion = create_conversion(bob, bob_token, "a1", "Europe Trip")

        response = bob.post("/conversions/bulk-delete", data={
            "ids": [alice_conversion, bob_conversion], "csrf_token": bob_token,
        }, follow_redirects=False)
        assert response.status_code == 303
        # bob's own conversion is gone...
        assert bob.get(f"/conversions/{bob_conversion}").status_code == 404

    # ...but alice's is untouched
    assert app_client.get(f"/conversions/{alice_conversion}").status_code == 200


@respx.mock
def test_bulk_delete_rejects_oversized_id_list(app_client):
    """An attacker-supplied `ids` list can't be used to make one request churn
    through an unbounded number of deletes — the route caps it instead."""
    from app.routes.conversions import _MAX_BULK_DELETE

    mock_budgets()
    token = login(app_client)
    conversion_id = create_conversion(app_client, token, "a1", "Japan Trip")

    oversized = [f"fake-id-{i}" for i in range(_MAX_BULK_DELETE + 1)]
    response = app_client.post(
        "/conversions/bulk-delete", data={"ids": oversized, "csrf_token": token}
    )
    assert response.status_code == 400
    # nothing was touched — the real conversion survives
    assert app_client.get(f"/conversions/{conversion_id}").status_code == 200


@respx.mock
def test_bulk_delete_accepts_exactly_the_cap(app_client):
    """The boundary itself must stay usable — only over the cap is rejected."""
    from app.routes.conversions import _MAX_BULK_DELETE

    mock_budgets()
    token = login(app_client)
    create_conversion(app_client, token, "a1", "Japan Trip")

    at_cap = [f"fake-id-{i}" for i in range(_MAX_BULK_DELETE)]
    response = app_client.post(
        "/conversions/bulk-delete", data={"ids": at_cap, "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303


@respx.mock
def test_last_synced_recorded_on_preview(app_client):
    from datetime import date

    mock_budgets()
    respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": []}})
    )
    token = login(app_client)
    conversion_id = create_conversion(app_client, token, "a1", "Japan Trip")

    # never synced yet
    assert "never" in app_client.get("/conversions").text
    assert "never" in app_client.get(f"/conversions/{conversion_id}").text

    app_client.post(f"/conversions/{conversion_id}/preview", data={"csrf_token": token})

    today = date.today().isoformat()
    assert today in app_client.get(f"/conversions/{conversion_id}").text
    assert today in app_client.get("/conversions").text


@respx.mock
def test_last_synced_not_recorded_when_patch_fails(app_client):
    """A failed apply must not claim the conversion is synced — mark_synced
    only fires after update_transactions actually succeeds."""
    mock_budgets()
    respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [
            {"id": "t1", "date": "2024-01-05", "amount": -1817000,
             "payee_name": "Ramen", "memo": None, "deleted": False},
        ]}})
    )
    respx.patch(f"{YNAB}/budgets/b1/transactions").mock(return_value=Response(500))

    token = login(app_client)
    conversion_id = create_conversion(app_client, token, "a1", "Japan Trip")

    response = app_client.post(f"/conversions/{conversion_id}/apply", data={
        "selected": ["t1"], "action_t1": "convert",
        "original_t1": "-1817000", "amount_t1": "-15990", "memo_t1": "x",
        "csrf_token": token,
    })
    assert response.status_code == 502  # the generic YNAB-error page

    assert "never" in app_client.get(f"/conversions/{conversion_id}").text


def test_security_headers_present(app_client):
    response = app_client.get("/login")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_login_required(app_client):
    response = app_client.get("/conversions", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_healthz_public_and_reports_version(app_client):
    response = app_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "dev"}


def test_landing_page_is_public(app_client):
    response = app_client.get("/")
    assert response.status_code == 200
    assert "Sign up" in response.text
    assert "Log in" in response.text
    assert "exchange rate" in response.text
    # once logged in, / goes straight to the conversions list
    login(app_client)
    response = app_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/conversions"


def test_post_without_csrf_token_rejected(app_client):
    # no token at all
    assert app_client.post(
        "/login", data={"email": EMAIL, "password": PASSWORD}
    ).status_code == 403
    # wrong token
    get_csrf(app_client)
    response = app_client.post(
        "/login", data={"email": EMAIL, "password": PASSWORD, "csrf_token": "forged"}
    )
    assert response.status_code == 403
    # authenticated POSTs are protected too
    token = login(app_client)
    assert app_client.post("/conversions/x/delete", data={}).status_code == 403
    assert app_client.post(
        "/conversions/x/delete", data={"csrf_token": token}, follow_redirects=False
    ).status_code == 303  # valid token: passes CSRF, delete of unknown id just redirects


def test_non_ascii_csrf_token_is_403_not_500(app_client):
    get_csrf(app_client)  # session now has a real token
    response = app_client.post(
        "/login", data={"email": EMAIL, "password": PASSWORD, "csrf_token": "é"}
    )
    assert response.status_code == 403


def test_conversions_redirect_to_settings_until_ynab_connected(app_client):
    token = signup(app_client)
    # the index page renders, with a connect notice
    index = app_client.get("/conversions")
    assert index.status_code == 200
    assert "Connect your YNAB account first" in index.text
    # anything that needs the YNAB API redirects to settings
    response = app_client.get("/conversions/new", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    response = app_client.post(
        "/conversions", data={
            "budget_id": "b1", "budget_name": "My Budget",
            "account_id": "a1", "account_name": "Japan Trip",
            "from_currency": "JPY", "to_currency": "USD",
            "start_date": "2024-01-01", "csrf_token": token,
        }, follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    # once connected, the notice disappears
    connect_ynab(app_client, token)
    index = app_client.get("/conversions")
    assert "Connect your YNAB account first" not in index.text


def test_legacy_connection_is_treated_as_unconnected(app_client):
    """A pre-OAuth-only row (no refresh_token) must not act as a live credential.

    require_ynab redirects to /settings like having no connection at all, but
    with an explanatory ?error=reauth — silently disconnecting a real user
    with no explanation is the failure mode this guards against."""
    from app.config import get_settings
    from app.connections import ConnectionStore
    from app.users import UserStore, normalize_email

    signup(app_client)
    data_dir = get_settings().data_dir
    user = UserStore(data_dir).get_by_email(normalize_email(EMAIL))
    ConnectionStore(data_dir)._upsert(user.id, "pat", "legacy-token", None, None)

    response = app_client.get("/conversions/new", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?error=reauth"
    # the stale row is cleaned up as a side effect
    page = app_client.get(response.headers["location"])
    assert "Not connected" in page.text
    assert "had to be cleared" in page.text


def test_never_connected_redirects_without_reauth_message(app_client):
    """A user who was never connected gets the plain redirect — no false
    'your old connection was cleared' message when nothing existed at all."""
    signup(app_client)
    response = app_client.get("/conversions/new", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"


@respx.mock
def test_full_conversion_flow(app_client):
    mock_budgets()
    transactions_route = respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
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
    # every YNAB call carries the user's own token
    for call in transactions_route.calls:
        assert call.request.headers["Authorization"] == "Bearer test-token"

    applied = app_client.post(f"/conversions/{conversion_id}/apply", data={
        "selected": ["t1"],
        "action_t1": "convert",
        "original_t1": "-1817000",
        "amount_t1": "-15990",
        "memo_t1": "-1,817 JPY (FX rate: 0.0087987)",
        "csrf_token": token,
    })
    # apply redirects back to the detail page with a flash
    assert applied.status_code == 200
    assert str(applied.url).endswith(f"/conversions/{conversion_id}?applied=1")
    assert "1 transaction updated in YNAB" in applied.text
    # apply (not just preview) records last_synced too
    from datetime import date
    assert date.today().isoformat() in applied.text

    assert patch_route.called
    assert patch_route.calls[0].request.headers["Authorization"] == "Bearer test-token"
    body = json.loads(patch_route.calls[0].request.content)
    assert body == {"transactions": [
        {"id": "t1", "amount": -15990, "memo": "-1,817 JPY (FX rate: 0.0087987)"}
    ]}


@respx.mock
def test_already_in_budget_currency_and_skip_actions(app_client):
    mock_budgets()
    respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [
            # actually 2,919 USD entered as "2,919 JPY" — keep amount, memo the JPY value
            {"id": "t1", "date": "2024-01-05", "amount": 2919000,
             "payee_name": "Transfer : BMO Chequing", "memo": None, "deleted": False},
            # reconciliation already in USD — mark skipped, no JPY memo
            {"id": "t2", "date": "2024-01-05", "amount": -61000,
             "payee_name": "Reconciliation", "memo": None, "deleted": False},
            # previously skipped: must never reappear in the preview
            {"id": "t3", "date": "2024-01-05", "amount": -5000000,
             "payee_name": "Old reconciliation", "memo": "(skipped)", "deleted": False},
        ]}})
    )
    respx.get(f"{FX}/2023-12-29..2024-01-05").mock(
        return_value=Response(200, json={"rates": {"2024-01-05": {"USD": 0.0087987}}})
    )
    patch_route = respx.patch(f"{YNAB}/budgets/b1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [{"id": "t1"}, {"id": "t2"}]}})
    )

    token = login(app_client)
    response = app_client.post("/conversions", data={
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a1", "account_name": "Japan Trip",
        "from_currency": "JPY", "to_currency": "USD",
        "start_date": "2024-01-01", "csrf_token": token,
    }, follow_redirects=False)
    conversion_id = response.headers["location"].rsplit("/", 1)[-1]

    preview = app_client.post(
        f"/conversions/{conversion_id}/preview", data={"csrf_token": token}
    )
    assert preview.status_code == 200
    assert 'value="t3"' not in preview.text  # already marked (skipped)
    # ...and the exclusion is surfaced with the affected payee, not silent
    assert "1 transaction marked" in preview.text
    assert "is excluded" in preview.text
    assert "Old reconciliation (2024-01-05)" in preview.text
    assert 'name="action_t1"' in preview.text
    # the action options lead with the budget currency ("Already USD")
    assert "Already USD (memo ≈331,754 JPY)" in preview.text
    assert "Already USD (skip forever)" in preview.text
    # 2,919 USD / 0.0087987 = 331,754 JPY offered as the already-USD memo
    assert 'name="already_memo_t1" value="≈ 331,754 JPY (FX rate: 0.0087987)"' in preview.text
    assert 'name="skip_memo_t2" value="(skipped)"' in preview.text

    applied = app_client.post(f"/conversions/{conversion_id}/apply", data={
        "selected": ["t1", "t2"],
        "action_t1": "already", "original_t1": "2919000",
        "amount_t1": "25680", "memo_t1": "unused",
        "already_memo_t1": "≈ 331,754 JPY (FX rate: 0.0087987)",
        "skip_memo_t1": "(skipped)",
        "action_t2": "skip", "original_t2": "-61000",
        "amount_t2": "-540", "memo_t2": "unused",
        "already_memo_t2": "≈ -6,933 JPY (FX rate: 0.0087987)",
        "skip_memo_t2": "(skipped)",
        "csrf_token": token,
    })
    assert applied.status_code == 200
    assert "2 transactions updated in YNAB" in applied.text

    # both PATCHes are memo-only: the amounts were already in the budget currency
    body = json.loads(patch_route.calls[0].request.content)
    assert body == {"transactions": [
        {"id": "t1", "memo": "≈ 331,754 JPY (FX rate: 0.0087987)"},
        {"id": "t2", "memo": "(skipped)"},
    ]}


@respx.mock
def test_from_currency_is_escaped_in_preview_script(app_client):
    # from_currency isn't validated on create, and it flows into an inline
    # <script>; it must be JSON-escaped so a crafted value can't break out of
    # the JS string or inject markup.
    mock_budgets()
    respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [
            {"id": "t1", "date": "2024-01-05", "amount": -1817000,
             "payee_name": "Ramen", "memo": None, "deleted": False},
        ]}})
    )
    respx.get(f"{FX}/2023-12-29..2024-01-05").mock(
        return_value=Response(200, json={"rates": {"2024-01-05": {"USD": 0.0087987}}})
    )
    token = login(app_client)
    response = app_client.post("/conversions", data={
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a1", "account_name": "Japan Trip",
        "from_currency": "`+alert(1)+`</script>", "to_currency": "USD",
        "start_date": "2024-01-01", "csrf_token": token,
    }, follow_redirects=False)
    conversion_id = response.headers["location"].rsplit("/", 1)[-1]
    preview = app_client.post(
        f"/conversions/{conversion_id}/preview", data={"csrf_token": token}
    )
    assert preview.status_code == 200
    # create upper-cases the currency, so the injected tag would be </SCRIPT>;
    # the page's own legit closing tag is lower-case </script>. The uppercase
    # raw tag must be absent — browsers close </SCRIPT> case-insensitively, so
    # tojson's < escaping (not .upper()) is what actually defuses it.
    assert "</SCRIPT>" not in preview.text
    assert 'const fromCurrency = "' in preview.text       # rendered as a JS string
    assert "\\u003c/SCRIPT\\u003e" in preview.text          # < and > escaped


@respx.mock
def test_apply_rejects_unknown_action(app_client):
    # the 400 fires while parsing the form, before any transactions fetch
    mock_budgets()
    token = login(app_client)
    response = app_client.post("/conversions", data={
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a1", "account_name": "Japan Trip",
        "from_currency": "JPY", "to_currency": "USD",
        "start_date": "2024-01-01", "csrf_token": token,
    }, follow_redirects=False)
    conversion_id = response.headers["location"].rsplit("/", 1)[-1]

    response = app_client.post(f"/conversions/{conversion_id}/apply", data={
        "selected": ["t1"], "action_t1": "explode",
        "amount_t1": "1", "memo_t1": "x", "csrf_token": token,
    })
    assert response.status_code == 400


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
    assert "0 transactions updated in YNAB" in applied.text
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


@respx.mock
def test_users_cannot_see_each_others_conversions(app_client):
    from fastapi.testclient import TestClient

    mock_budgets()
    alice_token = login(app_client, email="alice@example.com")
    response = app_client.post("/conversions", data={
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a1", "account_name": "Japan Trip",
        "from_currency": "JPY", "to_currency": "USD",
        "start_date": "2024-01-01", "csrf_token": alice_token,
    }, follow_redirects=False)
    conversion_id = response.headers["location"].rsplit("/", 1)[-1]

    with TestClient(app_client.app) as bob:
        bob_token = login(bob, email="bob@example.com")
        # bob's list is empty and alice's conversion is invisible to him
        assert "Japan Trip" not in bob.get("/conversions").text
        assert bob.get(f"/conversions/{conversion_id}").status_code == 404
        assert bob.get(f"/conversions/{conversion_id}/edit").status_code == 404
        assert bob.post(
            f"/conversions/{conversion_id}/preview", data={"csrf_token": bob_token}
        ).status_code == 404
        # apply is the one route that writes to YNAB — lock its IDOR guard too
        assert bob.post(f"/conversions/{conversion_id}/apply", data={
            "selected": ["t1"], "action_t1": "skip", "original_t1": "-1000",
            "amount_t1": "-1", "memo_t1": "x", "skip_memo_t1": "(skipped)",
            "csrf_token": bob_token,
        }).status_code == 404
        # bob can even use the same YNAB account id — conversions are per user
        assert bob.post("/conversions", data={
            "budget_id": "b1", "budget_name": "My Budget",
            "account_id": "a1", "account_name": "Japan Trip",
            "from_currency": "JPY", "to_currency": "USD",
            "start_date": "2024-01-01", "csrf_token": bob_token,
        }, follow_redirects=False).status_code == 303
        # a delete attempt on alice's conversion is a scoped no-op
        bob.post(
            f"/conversions/{conversion_id}/delete",
            data={"csrf_token": bob_token},
            follow_redirects=False,
        )

    # alice still has her conversion
    assert app_client.get(f"/conversions/{conversion_id}").status_code == 200


@respx.mock
def test_ynab_401_redirects_to_reconnect(app_client_factory):
    """A documented YNAB 401 (token revoked/expired) on a data call must guide
    the user to reconnect, not show a generic 'try again shortly' error page.
    The proactive refresh keeps tokens fresh, so a 401 here means a dead grant."""
    respx.get(f"{YNAB}/budgets").mock(
        return_value=Response(401, json={"error": {
            "id": "401", "name": "unauthorized", "detail": "Not authorized"}})
    )
    with app_client_factory(YNAB_CLIENT_ID="cid", YNAB_CLIENT_SECRET="csec") as app_client:
        login(app_client)
        # /conversions/new loads budgets from YNAB; the 401 short-circuits to settings
        response = app_client.get("/conversions/new", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/settings?error=revoked"
        page = app_client.get(response.headers["location"])
        assert "revoked" in page.text.lower()
        # the dead connection must be cleared, not just flagged — otherwise the
        # page renders "Connected" with no way back in except Disconnect-then-
        # reconnect, contradicting its own "please reconnect" message
        assert "Not connected" in page.text
        assert 'href="/oauth/ynab/start"' in page.text


@respx.mock
def test_malformed_start_date_is_400_not_500(app_client):
    mock_budgets()
    token = login(app_client)
    # a tampered/malformed start_date must be a 400, like the apply route,
    # not an unhandled 500
    response = app_client.post("/conversions", data={
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": "a1", "account_name": "Japan Trip",
        "from_currency": "JPY", "to_currency": "USD",
        "start_date": "not-a-date", "csrf_token": token,
    })
    assert response.status_code == 400
    assert "valid YYYY-MM-DD" in response.text
