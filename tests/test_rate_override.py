"""Manual per-row rate override in the preview: editing a rate and reposting
to the preview endpoint recomputes that row's amount and memo marker."""
import respx
from httpx import Response
from test_app_flow import FX, YNAB, create_conversion, login, mock_budgets


def _one_txn():
    return respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [
            {"id": "t1", "date": "2024-01-05", "amount": -1817000,
             "payee_name": "Ramen", "memo": None, "deleted": False},
        ]}})
    )


def _market_rate():
    return respx.get(f"{FX}/2023-12-29..2024-01-05").mock(
        return_value=Response(200, json={"rates": {"2024-01-05": {"USD": 0.0087987}}})
    )


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
    """A blank, non-numeric, or non-positive rate falls back to the market rate
    rather than producing a nonsense conversion."""
    mock_budgets()
    _one_txn()
    _market_rate()
    token = login(app_client)
    cid = create_conversion(app_client, token, "a1", "Japan Trip")

    for bad in ("abc", "0", "-1", ""):
        resp = app_client.post(
            f"/conversions/{cid}/preview",
            data={"csrf_token": token, "rate_t1": bad},
        )
        assert 'value="0.0087987"' in resp.text  # market rate kept
        assert "overridden" not in resp.text
        assert 'name="amount_t1" value="-15990"' in resp.text
