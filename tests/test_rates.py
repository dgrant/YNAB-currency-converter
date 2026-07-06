from datetime import date

import pytest
import respx
from httpx import Response

from app.rates import FrankfurterClient, RatesError, RateTable

BASE = "https://api.frankfurter.dev/v1"


@respx.mock
def test_time_series_fetch_includes_lookback():
    route = respx.get(f"{BASE}/2023-12-29..2024-01-08").mock(
        return_value=Response(200, json={
            "base": "JPY",
            "rates": {
                "2024-01-04": {"USD": 0.0087000},
                "2024-01-05": {"USD": 0.0087987},
                "2024-01-08": {"USD": 0.0088100},
            },
        })
    )
    table = FrankfurterClient(BASE).get_rates("JPY", "USD", date(2024, 1, 5), date(2024, 1, 8))
    assert route.called
    assert route.calls[0].request.url.params["base"] == "JPY"
    assert route.calls[0].request.url.params["symbols"] == "USD"
    assert table.rate_for(date(2024, 1, 5)) == 0.0087987
    # Saturday/Sunday fall back to Friday
    assert table.rate_for(date(2024, 1, 6)) == 0.0087987
    assert table.rate_for(date(2024, 1, 7)) == 0.0087987


def test_same_currency_is_identity():
    table = FrankfurterClient(BASE).get_rates("USD", "USD", date(2024, 1, 1), date(2024, 1, 2))
    assert table.rate_for(date(2024, 1, 1)) == 1.0


def test_missing_rate_raises():
    table = RateTable({"2024-01-05": 0.0088})
    with pytest.raises(RatesError):
        table.rate_for(date(2024, 2, 1))


@respx.mock
def test_api_error_raises():
    respx.get(f"{BASE}/2023-12-25..2024-01-02").mock(return_value=Response(404, text="not found"))
    with pytest.raises(RatesError) as excinfo:
        FrankfurterClient(BASE).get_rates("JPY", "XXX", date(2024, 1, 1), date(2024, 1, 2))
    # the error names the currency pair, so the failing conversion is identifiable
    assert "JPY->XXX" in str(excinfo.value)


def test_zero_or_negative_rate_is_rejected():
    # A bad rate from the API must fail loudly, not zero out conversions or
    # crash the inverse-equivalent division downstream.
    with pytest.raises(RatesError, match="Invalid exchange rate"):
        RateTable({"2024-01-05": 0.0}).rate_for(date(2024, 1, 5))
    with pytest.raises(RatesError, match="Invalid exchange rate"):
        RateTable({"2024-01-05": -0.5}).rate_for(date(2024, 1, 5))


def test_nan_and_infinity_rates_are_rejected():
    # json.loads parses bare NaN/Infinity, and NaN <= 0 is False — the guard
    # must catch non-finite rates explicitly.
    for bad in (float("nan"), float("inf")):
        with pytest.raises(RatesError, match="Invalid exchange rate"):
            RateTable({"2024-01-05": bad}).rate_for(date(2024, 1, 5))
