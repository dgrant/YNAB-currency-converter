"""The dashboard flow: preview-all (grouped) and apply-all (per-conversion),
plus the pending-count badges and the opt-in on-load refresh.

Reuses the mock/login helpers from the single-flow suite.
"""
import json
from datetime import date

import respx
from httpx import Response
from test_app_flow import (
    FX,
    YNAB,
    create_conversion,
    login,
    mock_budgets,
    mock_categories,
)


def _create_with_category(client, token, account_id, account_name,
                          from_currency="JPY", category_id="cat2", approve=True):
    """Create a conversion with a default category + approve flag (create
    validates against get_categories, so mock_categories must be set)."""
    data = {
        "budget_id": "b1", "budget_name": "My Budget",
        "account_id": account_id, "account_name": account_name,
        "from_currency": from_currency, "to_currency": "USD",
        "start_date": "2024-01-01", "default_category_id": category_id,
        "csrf_token": token,
    }
    if approve:
        data["approve_on_apply"] = "on"
    r = client.post("/conversions", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].rsplit("/", 1)[-1]


def _echo_patch(request):
    """respx side effect: a successful PATCH that confirms exactly what was sent."""
    body = json.loads(request.content)
    return Response(200, json={"data": {"transactions": body["transactions"]}})


def _txn(txn_id, amount=-1817000, dt="2024-01-05", memo=None, payee="Ramen", **extra):
    return {
        "id": txn_id, "date": dt, "amount": amount,
        "payee_name": payee, "memo": memo, "deleted": False, **extra,
    }


def _mock_txns(account_id, transactions):
    return respx.get(f"{YNAB}/budgets/b1/accounts/{account_id}/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": transactions}})
    )


def _mock_rates():
    return respx.get(f"{FX}/2023-12-29..2024-01-05").mock(
        return_value=Response(200, json={"rates": {"2024-01-05": {"USD": 0.0087987}}})
    )


def _conversion_row(conversion_id, email="user@example.com"):
    from app.config import get_settings
    from app.store import ConversionStore
    from app.users import UserStore, normalize_email

    data_dir = get_settings().data_dir
    user = UserStore(data_dir).get_by_email(normalize_email(email))
    return ConversionStore(data_dir).get(user.id, conversion_id)


@respx.mock
def test_preview_all_groups_every_account(app_client):
    mock_budgets()
    _mock_rates()
    _mock_txns("a1", [_txn("t1")])
    _mock_txns("a2", [_txn("t2", payee="Sushi")])
    token = login(app_client)
    create_conversion(app_client, token, "a1", "Japan Trip")
    create_conversion(app_client, token, "a2", "Kyoto Trip")

    r = app_client.post("/conversions/preview-all", data={"csrf_token": token})
    assert r.status_code == 200
    # both accounts grouped, each row present under its own selected_<cid> name
    assert "Japan Trip" in r.text and "Kyoto Trip" in r.text
    assert 'value="t1"' in r.text and 'value="t2"' in r.text
    assert "2 unconverted transaction" in r.text
    assert "across 2 account" in r.text


@respx.mock
def test_preview_all_no_conversions_redirects(app_client):
    mock_budgets()
    token = login(app_client)
    r = app_client.post(
        "/conversions/preview-all", data={"csrf_token": token}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/conversions"


@respx.mock
def test_preview_all_all_splits_group_shows_caught_up(app_client):
    """A conversion whose only txn is a split renders as 'nothing pending —
    checked', never omitted, and its badge is stored as 0."""
    mock_budgets()
    _mock_txns("a1", [_txn("t1", subtransactions=[{"amount": -1817000}])])
    token = login(app_client)
    cid = create_conversion(app_client, token, "a1", "Split Trip")

    r = app_client.post("/conversions/preview-all", data={"csrf_token": token})
    assert r.status_code == 200
    assert "caught up" in r.text
    assert _conversion_row(cid)["pending_count"] == 0


@respx.mock
def test_preview_all_one_group_rate_outage_isolated(app_client):
    """A RatesError on one conversion fails just that group; the other renders."""
    mock_budgets()
    _mock_txns("a1", [_txn("t1")])
    _mock_txns("a2", [_txn("t2", payee="Sushi")])
    # a1's currency is JPY, a2 we make EUR so the rate ranges hit different URLs
    respx.get(f"{FX}/2023-12-29..2024-01-05", params={"base": "JPY", "symbols": "USD"}).mock(
        return_value=Response(200, json={"rates": {"2024-01-05": {"USD": 0.0087987}}})
    )
    respx.get(f"{FX}/2023-12-29..2024-01-05", params={"base": "EUR", "symbols": "USD"}).mock(
        return_value=Response(500, text="rates down")
    )
    token = login(app_client)
    create_conversion(app_client, token, "a1", "Japan Trip", from_currency="JPY")
    create_conversion(app_client, token, "a2", "Euro Trip", from_currency="EUR")

    r = app_client.post("/conversions/preview-all", data={"csrf_token": token})
    assert r.status_code == 200
    assert 'value="t1"' in r.text            # good group rendered
    assert "Could not check this account" in r.text  # failed group rendered
    assert "Retry Euro Trip" in r.text


@respx.mock
def test_preview_all_401_reconnects(app_client):
    """A revoked token (401) mid-loop routes to the reconnect flow, not a
    per-group error row."""
    mock_budgets()
    respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(401, json={"error": {"detail": "unauthorized"}})
    )
    token = login(app_client)
    create_conversion(app_client, token, "a1", "Japan Trip")

    r = app_client.post(
        "/conversions/preview-all", data={"csrf_token": token}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?error=revoked"


@respx.mock
def test_preview_all_429_aborts_loop(app_client):
    """A 429 stops the loop instead of firing more requests into a
    rate-limited API."""
    mock_budgets()
    respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(429)
    )
    a2_route = _mock_txns("a2", [_txn("t2")])
    token = login(app_client)
    create_conversion(app_client, token, "a1", "First")
    create_conversion(app_client, token, "a2", "Second")

    r = app_client.post("/conversions/preview-all", data={"csrf_token": token})
    assert r.status_code == 429
    assert not a2_route.called  # loop aborted before touching the second account


@respx.mock
def test_apply_all_partial_failure(app_client):
    """A succeeds, B's PATCH fails: A is applied + synced, B is reported failed
    and left untouched, and the flash summarises both."""
    mock_budgets()
    _mock_rates()
    _mock_txns("a1", [_txn("t1")])
    _mock_txns("a2", [_txn("t2", payee="Sushi")])
    patched = {"count": 0}

    def patch_side_effect(request):
        patched["count"] += 1
        # first PATCH (account a1) ok, second (a2) fails
        if patched["count"] == 1:
            return _echo_patch(request)
        return Response(500, text="boom")

    respx.patch(f"{YNAB}/budgets/b1/transactions").mock(side_effect=patch_side_effect)
    token = login(app_client)
    cid_a = create_conversion(app_client, token, "a1", "Japan Trip")
    cid_b = create_conversion(app_client, token, "a2", "Kyoto Trip")

    r = app_client.post("/conversions/apply-all", data={
        "csrf_token": token,
        "conversion_ids": [cid_a, cid_b],
        f"selected_{cid_a}": ["t1"],
        "action_t1": "convert", "original_t1": "-1817000",
        "amount_t1": "-15990", "memo_t1": "-1,817 JPY (FX rate: 0.0087987)",
        f"selected_{cid_b}": ["t2"],
        "action_t2": "convert", "original_t2": "-1817000",
        "amount_t2": "-15990", "memo_t2": "-1,817 JPY (FX rate: 0.0087987)",
    })
    assert r.status_code == 200  # redirected to the index dashboard
    assert "Japan Trip: 1 applied" in r.text
    assert "Kyoto Trip: failed" in r.text
    # A advanced last_synced; B did not (write-after-success)
    assert _conversion_row(cid_a)["last_synced"] == date.today().isoformat()
    assert _conversion_row(cid_b)["last_synced"] is None


@respx.mock
def test_apply_all_per_group_category_and_approve(app_client):
    """Apply-all is per-group: account A (default category + approve) sends
    category_id + approved and drops the category on its transfer row; account B
    (no default, no approve) sends neither — proving these aren't global flags.
    Also proves the shared category cache means one categories fetch, not one
    per group, and that dropped_categories is surfaced in the summary."""
    mock_budgets()
    mock_categories()
    cat_route = respx.get(f"{YNAB}/budgets/b1/categories").mock(
        return_value=Response(200, json={"data": {"category_groups": [
            {"id": "cg1", "name": "Everyday", "deleted": False, "hidden": False,
             "categories": [
                 {"id": "cat2", "name": "2026 Japan Vacation", "deleted": False, "hidden": False},
             ]},
        ]}})
    )
    _mock_rates()
    _mock_txns("a1", [_txn("t1"), _txn("tt", payee="Wire", transfer_account_id="acct-x")])
    _mock_txns("a2", [_txn("t2", payee="Sushi")])
    patch_route = respx.patch(f"{YNAB}/budgets/b1/transactions").mock(side_effect=_echo_patch)
    token = login(app_client)
    cid_a = _create_with_category(app_client, token, "a1", "Japan Trip")
    cid_b = create_conversion(app_client, token, "a2", "Kyoto Trip")  # no category/approve
    before = cat_route.call_count  # ignore create-time validation fetches

    r = app_client.post("/conversions/apply-all", data={
        "csrf_token": token,
        "conversion_ids": [cid_a, cid_b],
        f"default_category_id_{cid_a}": "cat2", f"approve_{cid_a}": "on",
        f"selected_{cid_a}": ["t1", "tt"],
        "action_t1": "convert", "original_t1": "-1817000",
        "amount_t1": "-15990", "memo_t1": "m1",
        "action_tt": "convert", "original_tt": "-1817000",
        "amount_tt": "-15990", "memo_tt": "m-tt",
        f"selected_{cid_b}": ["t2"],
        "action_t2": "convert", "original_t2": "-1817000",
        "amount_t2": "-15990", "memo_t2": "m2",
    })
    assert r.status_code == 200
    by_id = {}
    for call in patch_route.calls:
        for row in json.loads(call.request.content)["transactions"]:
            by_id[row["id"]] = row
    # A's normal row: categorized + approved
    assert by_id["t1"]["category_id"] == "cat2" and by_id["t1"]["approved"] is True
    # A's transfer row: approved but category dropped
    assert "category_id" not in by_id["tt"] and by_id["tt"]["approved"] is True
    # B's row: no category, no approve (per-group isolation, not global)
    assert "category_id" not in by_id["t2"] and "approved" not in by_id["t2"]
    # apply-time: only A had a category, so exactly one apply-time fetch
    assert cat_route.call_count - before == 1
    # the summary reports A's categorized + dropped + approved
    assert "Japan Trip: 2 applied, 1 categorized, 1 left uncategorized, 2 approved" in r.text


@respx.mock
def test_apply_all_shares_category_cache_across_budget(app_client):
    """Two accounts in the same budget both categorizing: apply-all fetches that
    budget's category list once (shared cache), not once per group."""
    mock_budgets()
    mock_categories()
    cat_route = respx.get(f"{YNAB}/budgets/b1/categories").mock(
        return_value=Response(200, json={"data": {"category_groups": [
            {"id": "cg1", "name": "Everyday", "deleted": False, "hidden": False,
             "categories": [
                 {"id": "cat2", "name": "2026 Japan Vacation", "deleted": False, "hidden": False},
             ]},
        ]}})
    )
    _mock_rates()
    _mock_txns("a1", [_txn("t1")])
    _mock_txns("a2", [_txn("t2", payee="Sushi")])
    respx.patch(f"{YNAB}/budgets/b1/transactions").mock(side_effect=_echo_patch)
    token = login(app_client)
    cid_a = _create_with_category(app_client, token, "a1", "Japan Trip")
    cid_b = _create_with_category(app_client, token, "a2", "Kyoto Trip")
    before = cat_route.call_count

    app_client.post("/conversions/apply-all", data={
        "csrf_token": token,
        "conversion_ids": [cid_a, cid_b],
        f"default_category_id_{cid_a}": "cat2", f"selected_{cid_a}": ["t1"],
        "action_t1": "convert", "original_t1": "-1817000", "amount_t1": "-15990", "memo_t1": "m1",
        f"default_category_id_{cid_b}": "cat2", f"selected_{cid_b}": ["t2"],
        "action_t2": "convert", "original_t2": "-1817000", "amount_t2": "-15990", "memo_t2": "m2",
    })
    # both groups validated their category against ONE fetch, not two
    assert cat_route.call_count - before == 1


@respx.mock
def test_apply_all_cross_group_tamper_dropped(app_client):
    """Posting account A's txn id under account B's group never patches it into
    B — B's own re-fetch (present_ids) doesn't contain it, so it's dropped."""
    mock_budgets()
    _mock_txns("a1", [_txn("t1")])
    _mock_txns("a2", [_txn("t2", payee="Sushi")])
    patch_route = respx.patch(f"{YNAB}/budgets/b1/transactions").mock(side_effect=_echo_patch)
    token = login(app_client)
    create_conversion(app_client, token, "a1", "Japan Trip")
    cid_b = create_conversion(app_client, token, "a2", "Kyoto Trip")

    # Post t1 (which lives in account a1) under account b's group only.
    app_client.post("/conversions/apply-all", data={
        "csrf_token": token,
        "conversion_ids": [cid_b],
        f"selected_{cid_b}": ["t1"],
        "action_t1": "skip", "original_t1": "-1817000",
        "skip_memo_t1": "(skipped)",
    })
    # t1 isn't in a2's fetched txns, so it must never appear in a PATCH.
    for call in patch_route.calls:
        body = json.loads(call.request.content)
        assert all(t["id"] != "t1" for t in body["transactions"])


@respx.mock
def test_badge_count_matches_next_preview(app_client):
    """After apply, the stored badge count equals the convertible count the
    next preview would show (t2 left unticked stays pending)."""
    mock_budgets()
    _mock_rates()
    _mock_txns("a1", [_txn("t1"), _txn("t2", dt="2024-01-05", payee="Sushi")])
    respx.patch(f"{YNAB}/budgets/b1/transactions").mock(side_effect=_echo_patch)
    token = login(app_client)
    cid = create_conversion(app_client, token, "a1", "Japan Trip")

    # Apply only t1, leave t2 pending.
    app_client.post(f"/conversions/{cid}/apply", data={
        "csrf_token": token, "selected": ["t1"],
        "action_t1": "convert", "original_t1": "-1817000",
        "amount_t1": "-15990", "memo_t1": "-1,817 JPY (FX rate: 0.0087987)",
    })
    # Badge says 1 pending (t2). And t1 now carries the marker, so the next
    # preview would also show exactly t2.
    assert _conversion_row(cid)["pending_count"] == 1


@respx.mock
def test_preview_all_renders_editable_rate(app_client):
    """The dashboard preview shows each rate as an editable rate_<id> input and
    a 'Recompute with my rates' button, matching the single-preview page."""
    mock_budgets()
    _mock_rates()
    _mock_txns("a1", [_txn("t1")])
    token = login(app_client)
    create_conversion(app_client, token, "a1", "Japan Trip")

    r = app_client.post("/conversions/preview-all", data={"csrf_token": token})
    assert r.status_code == 200
    assert 'name="rate_t1"' in r.text
    assert 'value="0.0087987"' in r.text            # market rate prefilled
    assert "Recompute with my rates" in r.text
    assert "overridden" not in r.text               # nothing changed yet


@respx.mock
def test_preview_all_applies_manual_rate_override(app_client):
    """Reposting the dashboard preview with a manual rate recomputes that row's
    amount and memo — the same override support the single-preview page has,
    and the fix for 'override only works on the conversion-specific page'."""
    mock_budgets()
    _mock_rates()
    _mock_txns("a1", [_txn("t1"), _txn("t2", payee="Sushi")])
    token = login(app_client)
    create_conversion(app_client, token, "a1", "Japan Trip")

    # t1 overridden to 0.009; t2 left at its prefilled market rate.
    r = app_client.post("/conversions/preview-all", data={
        "csrf_token": token, "rate_t1": "0.009", "rate_t2": "0.0087987",
    })
    assert r.status_code == 200
    # Only t1 is flagged overridden (the reposted t2 rate matches the market).
    assert r.text.count("rate-input overridden") == 1
    # t1 recomputed at the manual rate; t2 stays at the market amount.
    assert 'name="amount_t1" value="-16350"' in r.text
    assert 'name="amount_t2" value="-15990"' in r.text
    assert "-1,817 JPY (FX rate: 0.009)" in r.text   # memo marker carries new rate


@respx.mock
def test_preview_all_requires_csrf(app_client):
    mock_budgets()
    token = login(app_client)
    create_conversion(app_client, token, "a1", "Japan Trip")
    r = app_client.post("/conversions/preview-all", data={})
    assert r.status_code == 403


@respx.mock
def test_apply_all_requires_csrf(app_client):
    mock_budgets()
    token = login(app_client)
    create_conversion(app_client, token, "a1", "Japan Trip")
    r = app_client.post("/conversions/apply-all", data={"conversion_ids": ["x"]})
    assert r.status_code == 403


@respx.mock
def test_refresh_on_load_default_off_no_fetch(app_client):
    """Default OFF: opening the dashboard makes zero YNAB requests."""
    mock_budgets()
    txns = _mock_txns("a1", [_txn("t1")])
    token = login(app_client)
    create_conversion(app_client, token, "a1", "Japan Trip")
    # creating the conversion validates the budget (get_budgets), but must not
    # fetch transactions on the plain index load
    txns.reset()
    app_client.get("/conversions")
    assert not txns.called


@respx.mock
def test_refresh_on_load_survives_token_outage(app_client, monkeypatch):
    """Regression: an opted-in on-load refresh must be best-effort — a transient
    token-endpoint outage (get_access_token raising) leaves cached counts in
    place and still renders the dashboard, never a 502 error page."""
    from app.ynab import YNABError

    mock_budgets()
    _mock_txns("a1", [_txn("t1")])
    token = login(app_client)
    create_conversion(app_client, token, "a1", "Japan Trip")
    app_client.post(
        "/settings/refresh-on-load", data={"csrf_token": token, "enabled": "on"}
    )

    def _boom(*_a, **_k):
        raise YNABError("token endpoint 502", status_code=502)

    monkeypatch.setattr("app.routes.conversions.oauth.get_access_token", _boom)
    r = app_client.get("/conversions")
    assert r.status_code == 200


@respx.mock
def test_refresh_on_load_when_enabled_refreshes_stale(app_client):
    """Toggle ON: a never-checked conversion gets its count fetched on load."""
    mock_budgets()
    txns = _mock_txns("a1", [_txn("t1")])
    token = login(app_client)
    cid = create_conversion(app_client, token, "a1", "Japan Trip")
    app_client.post(
        "/settings/refresh-on-load", data={"csrf_token": token, "enabled": "on"}
    )
    txns.reset()
    r = app_client.get("/conversions")
    assert txns.called                       # stale (never-checked) → refreshed
    assert _conversion_row(cid)["pending_count"] == 1
    assert "1" in r.text
