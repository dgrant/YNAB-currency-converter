"""Manual per-row rate override in the preview: editing a rate and reposting
to the preview endpoint recomputes that row's amount and memo marker."""
import respx
from httpx import Response
from test_app_flow import (
    FX,
    YNAB,
    assert_sortable_markup,
    create_conversion,
    login,
    mock_budgets,
)


def _one_txn():
    return respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [
            {"id": "t1", "date": "2024-01-05", "amount": -1817000,
             "payee_name": "Ramen", "memo": None, "deleted": False},
        ]}})
    )


def _two_txns():
    return respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [
            {"id": "t1", "date": "2024-01-05", "amount": -1817000,
             "payee_name": "Ramen", "memo": None, "deleted": False},
            {"id": "t2", "date": "2024-01-05", "amount": -5000000,
             "payee_name": "Hotel", "memo": None, "deleted": False},
        ]}})
    )


def _market_rate():
    return respx.get(f"{FX}/2023-12-29..2024-01-05").mock(
        return_value=Response(200, json={"rates": {"2024-01-05": {"USD": 0.0087987}}})
    )


@respx.mock
def test_preview_transaction_table_is_sortable(app_client):
    """The single-account preview table carries the client-side sort wiring
    (see app/static/sortable.js): the `sortable` class, clickable per-column
    headers, the raw numeric sort value on the amount cell, and the script
    include. The reorder itself is JS, exercised in a browser."""
    mock_budgets()
    _one_txn()
    _market_rate()
    token = login(app_client)
    cid = create_conversion(app_client, token, "a1", "Japan Trip")

    r = app_client.post(f"/conversions/{cid}/preview", data={"csrf_token": token})
    assert r.status_code == 200
    assert 'class="sortable"' in r.text
    assert_sortable_markup(r.text)


@respx.mock
def test_preview_applies_manual_rate_override(app_client):
    mock_budgets()
    _one_txn()
    _market_rate()
    token = login(app_client)
    cid = create_conversion(app_client, token, "a1", "Japan Trip")

    # Initial preview: market rate + amount, editable rate field, no override.
    initial = app_client.post(f"/conversions/{cid}/preview", data={"csrf_token": token})
    assert 'value="0.0087987"' in initial.text  # market rate in the editable cell
    assert "-15.99" in initial.text              # -1817 * 0.0087987 rounded
    assert "overridden" not in initial.text

    # Recompute with a manual rate of 0.009.
    recomputed = app_client.post(
        f"/conversions/{cid}/preview",
        data={"csrf_token": token, "rate_t1": "0.009"},
    )
    assert 'value="0.009"' in recomputed.text
    assert "rate-input overridden" in recomputed.text
    # -1817 * 0.009 = -16.353 -> -16.35, and the memo marker carries the new rate
    assert 'name="amount_t1" value="-16350"' in recomputed.text
    assert "-1,817 JPY (FX rate: 0.009)" in recomputed.text


@respx.mock
def test_preview_ignores_invalid_rate_override(app_client):
    """A blank, non-numeric, non-positive, or absurdly large rate falls back to
    the market rate rather than producing a nonsense conversion or a 500 (an
    enormous-but-finite rate like 1e300 would overflow the Decimal context in
    convert_milliunits if it weren't dropped up front)."""
    mock_budgets()
    _one_txn()
    _market_rate()
    token = login(app_client)
    cid = create_conversion(app_client, token, "a1", "Japan Trip")

    for bad in ("abc", "0", "-1", "", "1e300", "nan", "inf"):
        resp = app_client.post(
            f"/conversions/{cid}/preview",
            data={"csrf_token": token, "rate_t1": bad},
        )
        assert 'value="0.0087987"' in resp.text  # market rate kept
        assert "overridden" not in resp.text
        assert 'name="amount_t1" value="-15990"' in resp.text


@respx.mock
def test_recompute_flags_only_the_changed_row(app_client):
    """Regression (found by /qa in a browser): the preview form reposts every
    rate_<id> field, so a row left at its market rate must NOT be flagged as
    overridden just because a *different* row was changed."""
    mock_budgets()
    _two_txns()
    _market_rate()
    token = login(app_client)
    cid = create_conversion(app_client, token, "a1", "Japan Trip")

    # Recompute with t1 changed and t2 left at its prefilled market rate.
    resp = app_client.post(
        f"/conversions/{cid}/preview",
        data={"csrf_token": token, "rate_t1": "0.009", "rate_t2": "0.0087987"},
    )
    # Exactly one row carries the overridden class (t1), not both.
    assert resp.text.count("rate-input overridden") == 1
    # t1 recomputed at the manual rate; t2 stays at the market amount.
    assert 'name="amount_t1" value="-16350"' in resp.text
    assert 'name="amount_t2" value="-43990"' in resp.text
