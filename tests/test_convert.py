from datetime import date

from app.convert import (
    build_memo,
    build_preview,
    convert_milliunits,
    format_original,
    format_rate,
    is_converted,
)
from app.rates import RateTable


class TestMarkerDetection:
    def test_rmillan_format_is_detected(self):
        # Exact memo format produced by ynab.rmillan.com
        assert is_converted("-1,817 JPY (FX rate: 0.0087987)")

    def test_marker_appended_to_existing_memo(self):
        assert is_converted("lunch with team -1,817 JPY (FX rate: 0.0087987)")

    def test_plain_memo_is_not_converted(self):
        assert not is_converted("lunch with team")
        assert not is_converted("rate: 0.0087987")
        assert not is_converted("")
        assert not is_converted(None)

    def test_own_memo_roundtrips(self):
        memo = build_memo("coffee", -1817000, "JPY", 0.0087987)
        assert memo == "coffee -1,817 JPY (FX rate: 0.0087987)"
        assert is_converted(memo)

    def test_memo_without_existing_text(self):
        assert build_memo(None, -45300, "EUR", 1.0842) == "-45.30 EUR (FX rate: 1.0842)"


class TestFormatting:
    def test_zero_decimal_currency(self):
        assert format_original(-1817000, "JPY") == "-1,817 JPY"

    def test_two_decimal_currency(self):
        assert format_original(-45300, "EUR") == "-45.30 EUR"

    def test_positive_amount_with_thousands(self):
        assert format_original(1234567890, "USD") == "1,234,567.89 USD"

    def test_rate_formatting(self):
        assert format_rate(0.0087987) == "0.0087987"
        assert format_rate(1.0842) == "1.0842"
        assert format_rate(0.00000123) == "0.00000123"


class TestConversion:
    def test_rounds_to_target_minor_unit(self):
        # -1817 JPY * 0.0087987 = -15.9872... USD -> -15.99
        assert convert_milliunits(-1817000, 0.0087987, "USD") == -15990

    def test_to_zero_decimal_currency(self):
        # -15.99 USD * 113.65 = -1817.26 JPY -> -1817
        assert convert_milliunits(-15990, 113.65, "JPY") == -1817000

    def test_half_rounds_away_from_zero(self):
        assert convert_milliunits(1000, 0.005, "USD") == 10
        assert convert_milliunits(-1000, 0.005, "USD") == -10


class TestBuildPreview:
    def make_rates(self):
        return RateTable({"2024-01-05": 0.0087987, "2024-01-08": 0.0088100})

    def test_skips_converted_and_zero_transactions(self):
        transactions = [
            {"id": "t1", "date": "2024-01-05", "amount": -1817000,
             "payee_name": "Ramen", "memo": None},
            {"id": "t2", "date": "2024-01-05", "amount": -5000000,
             "payee_name": "Hotel", "memo": "-5,000 JPY (FX rate: 0.0087987)"},
            {"id": "t3", "date": "2024-01-05", "amount": 0,
             "payee_name": "Starting balance", "memo": None},
        ]
        rows = build_preview(transactions, self.make_rates(), "JPY", "USD")
        assert [r["id"] for r in rows] == ["t1"]
        assert rows[0]["new_milliunits"] == -15990
        assert rows[0]["new_memo"] == "-1,817 JPY (FX rate: 0.0087987)"

    def test_weekend_falls_back_to_prior_business_day(self):
        transactions = [
            {"id": "t1", "date": "2024-01-06", "amount": -1000000,
             "payee_name": "Saturday", "memo": None},
        ]
        rows = build_preview(transactions, self.make_rates(), "JPY", "USD")
        assert rows[0]["rate"] == 0.0087987  # Friday's rate

    def test_existing_memo_is_preserved(self):
        transactions = [
            {"id": "t1", "date": "2024-01-08", "amount": -1817000,
             "payee_name": "Ramen", "memo": "team lunch"},
        ]
        rows = build_preview(transactions, self.make_rates(), "JPY", "USD")
        assert rows[0]["new_memo"] == "team lunch -1,817 JPY (FX rate: 0.00881)"
