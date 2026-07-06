import math
from datetime import date, timedelta

import httpx

from .http import get_or_error

# Fetch a few extra days before the first transaction so weekend/holiday
# dates at the start of the range can fall back to a prior business day.
LOOKBACK_DAYS = 7


class RatesError(Exception):
    pass


class FrankfurterClient:
    """Historical FX rates from the free Frankfurter API (ECB data)."""

    def __init__(self, base_url: str = "https://api.frankfurter.dev/v1") -> None:
        self._client = httpx.Client(base_url=base_url, timeout=30)
        self._currencies: dict[str, str] | None = None

    def _get(self, path: str, params: dict | None = None, context: str = ""):
        response = get_or_error(
            self._client, path, params, RatesError, "the exchange-rate service"
        )
        if response.status_code != 200:
            raise RatesError(
                f"Frankfurter API error {response.status_code}{context}: {response.text}"
            )
        return response

    def currencies(self) -> dict[str, str]:
        if self._currencies is None:
            self._currencies = self._get("/currencies").json()
        return self._currencies

    def get_rates(
        self, from_currency: str, to_currency: str, start: date, end: date
    ) -> "RateTable":
        if from_currency == to_currency:
            return RateTable({}, same_currency=True)
        response = self._get(
            f"/{start - timedelta(days=LOOKBACK_DAYS)}..{end}",
            params={"base": from_currency, "symbols": to_currency},
            context=f" for {from_currency}->{to_currency}",
        )
        rates = {
            day: symbols[to_currency]
            for day, symbols in response.json()["rates"].items()
            if to_currency in symbols
        }
        if not rates:
            raise RatesError(f"No rates returned for {from_currency}->{to_currency}")
        return RateTable(rates)


class RateTable:
    """Rates keyed by ISO date; missing days fall back to the prior business day."""

    def __init__(self, rates: dict[str, float], same_currency: bool = False) -> None:
        self._rates = rates
        self._same_currency = same_currency

    def rate_for(self, day: date) -> float:
        if self._same_currency:
            return 1.0
        for _ in range(LOOKBACK_DAYS + 1):
            rate = self._rates.get(day.isoformat())
            if rate is not None:
                # A zero/negative/NaN/Infinity rate from the API would corrupt
                # conversions or crash the math — refuse it upfront. (json.loads
                # happily parses bare NaN/Infinity, and NaN <= 0 is False.)
                if rate <= 0 or not math.isfinite(rate):
                    raise RatesError(f"Invalid exchange rate {rate} for {day.isoformat()}")
                return rate
            day -= timedelta(days=1)
        raise RatesError(f"No rate available on or before {day}")
