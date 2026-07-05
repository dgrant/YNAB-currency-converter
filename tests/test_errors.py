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
def test_malformed_apply_form_is_a_400_not_a_500(app_client):
    token = login(app_client)
    conversion_id = make_conversion(app_client, token)
    # selected id with no action/amount/memo fields at all (tampered or stale form)
    response = app_client.post(f"/conversions/{conversion_id}/apply", data={
        "selected": ["t1"], "csrf_token": token,
    })
    assert response.status_code == 400
    assert "Malformed apply form" in response.text
    # action present but amount_/memo_ missing
    response = app_client.post(f"/conversions/{conversion_id}/apply", data={
        "selected": ["t1"], "action_t1": "convert", "csrf_token": token,
    })
    assert response.status_code == 400
    # non-numeric amount
    response = app_client.post(f"/conversions/{conversion_id}/apply", data={
        "selected": ["t1"], "action_t1": "convert",
        "amount_t1": "abc", "memo_t1": "x", "csrf_token": token,
    })
    assert response.status_code == 400
    # a pre-actions form (no action_ field) must not silently convert
    response = app_client.post(f"/conversions/{conversion_id}/apply", data={
        "selected": ["t1"], "amount_t1": "-15990", "memo_t1": "x", "csrf_token": token,
    })
    assert response.status_code == 400


@respx.mock
def test_malformed_already_and_skip_forms_are_a_400_not_a_500(app_client):
    # action=already/skip whose corresponding memo field is missing (tampered form)
    token = login(app_client)
    conversion_id = make_conversion(app_client, token)
    for action in ("already", "skip"):
        response = app_client.post(f"/conversions/{conversion_id}/apply", data={
            "selected": ["t1"], "action_t1": action, "csrf_token": token,
        })
        assert response.status_code == 400
        assert "Malformed apply form" in response.text


@respx.mock
def test_apply_recheck_skips_transactions_that_became_splits(app_client):
    # t1 was a normal transaction at preview time but is a split by apply time
    respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [
            {"id": "t1", "date": "2024-01-05", "amount": -1817000,
             "payee_name": "Ramen", "memo": None, "deleted": False,
             "subtransactions": [{"id": "s1", "amount": -1000000},
                                 {"id": "s2", "amount": -817000}]},
        ]}})
    )
    patch_route = respx.patch(f"{YNAB}/budgets/b1/transactions")
    token = login(app_client)
    conversion_id = make_conversion(app_client, token)
    response = app_client.post(f"/conversions/{conversion_id}/apply", data={
        "selected": ["t1"], "action_t1": "convert",
        "amount_t1": "-15990", "memo_t1": "x",
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("?applied=0&skipped_splits=1")
    assert not patch_route.called
    followed = app_client.get(response.headers["location"])
    assert "1 skipped" in followed.text

    # memo-only actions are dropped for became-splits too: conservative, and
    # pins the behavior either way
    response = app_client.post(f"/conversions/{conversion_id}/apply", data={
        "selected": ["t1"], "action_t1": "skip",
        "amount_t1": "-15990", "memo_t1": "x", "skip_memo_t1": "(skipped)",
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("?applied=0&skipped_splits=1")
    assert not patch_route.called


@respx.mock
def test_apply_drops_transactions_deleted_since_preview(app_client):
    # t1 was previewed but no longer exists in YNAB by apply time; t2 still does.
    respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [
            {"id": "t2", "date": "2024-01-05", "amount": -2000000,
             "payee_name": "Sushi", "memo": None, "deleted": False},
        ]}})
    )
    patch_route = respx.patch(f"{YNAB}/budgets/b1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [{"id": "t2"}]}})
    )
    token = login(app_client)
    conversion_id = make_conversion(app_client, token)
    response = app_client.post(f"/conversions/{conversion_id}/apply", data={
        "selected": ["t1", "t2"],
        "action_t1": "convert", "amount_t1": "-15990", "memo_t1": "x",
        "action_t2": "convert", "amount_t2": "-17600", "memo_t2": "y",
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    # only the still-present t2 is patched; the deleted t1 is dropped
    assert response.headers["location"].endswith("?applied=1")
    body = patch_route.calls[0].request.content.decode()
    assert '"t2"' in body and '"t1"' not in body


@respx.mock
def test_apply_drops_transactions_already_actioned_since_preview(app_client):
    # By apply time t1 was converted (FX marker) and t2 skipped in another
    # tab/session; a stale form resubmit must not clobber either decision.
    respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [
            {"id": "t1", "date": "2024-01-05", "amount": -15990,
             "payee_name": "Ramen", "memo": "-1,817 JPY (FX rate: 0.0087987)",
             "deleted": False},
            {"id": "t2", "date": "2024-01-05", "amount": -61000,
             "payee_name": "Reconciliation", "memo": "(skipped)", "deleted": False},
        ]}})
    )
    patch_route = respx.patch(f"{YNAB}/budgets/b1/transactions")
    token = login(app_client)
    conversion_id = make_conversion(app_client, token)
    response = app_client.post(f"/conversions/{conversion_id}/apply", data={
        "selected": ["t1", "t2"],
        "action_t1": "convert", "amount_t1": "-15990", "memo_t1": "stale memo",
        "action_t2": "convert", "amount_t2": "-540", "memo_t2": "stale memo",
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("?applied=0")
    assert not patch_route.called


def test_unhandled_exception_gets_friendly_500_with_headers(app_client):
    @app_client.app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    response = app_client.get("/boom")
    assert response.status_code == 500
    assert "Something went wrong" in response.text
    assert "kaboom" not in response.text  # no internals leaked
    assert response.headers["X-Frame-Options"] == "DENY"


def test_http_exceptions_render_error_page(app_client):
    login(app_client)
    response = app_client.get("/conversions/nope")
    assert response.status_code == 404
    assert "Not found" in response.text
    assert "Conversion not found" in response.text


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
