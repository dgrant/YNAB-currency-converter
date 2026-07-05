
from app.convert import (
    build_already_memo,
    build_memo,
    build_preview,
    build_skip_memo,
    convert_milliunits,
    equivalent_milliunits,
    format_amount,
    format_original,
    format_rate,
    is_converted,
    is_skipped,
    is_split,
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

    def test_skipped_marker_detected(self):
        assert is_skipped("(skipped)")
        assert is_skipped("reconciliation (skipped)")
        assert not is_skipped("skipped")
        assert not is_skipped("")
        assert not is_skipped(None)

    def test_skip_memo_roundtrips(self):
        assert build_skip_memo(None) == "(skipped)"
        memo = build_skip_memo("reconciliation")
        assert memo == "reconciliation (skipped)"
        assert is_skipped(memo)
        assert not is_converted(memo)

    def test_already_memo_roundtrips(self):
        # 2,919 CAD entered as "2,919 JPY" — memo records the true JPY equivalent
        memo = build_already_memo("transfer", 331328000, "JPY", 0.00881)
        assert memo == "transfer ≈ 331,328 JPY (FX rate: 0.00881)"
        assert is_converted(memo)
        assert build_already_memo(None, 331328000, "JPY", 0.00881) == (
            "≈ 331,328 JPY (FX rate: 0.00881)"
        )


class TestFormatting:
    def test_zero_decimal_currency(self):
        assert format_original(-1817000, "JPY") == "-1,817 JPY"

    def test_two_decimal_currency(self):
        assert format_original(-45300, "EUR") == "-45.30 EUR"

    def test_positive_amount_with_thousands(self):
        assert format_original(1234567890, "USD") == "1,234,567.89 USD"

    def test_amount_without_currency_suffix(self):
        assert format_amount(-1817000, "JPY") == "-1,817"
        assert format_amount(-45300, "EUR") == "-45.30"

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

    def test_equivalent_is_inverse_conversion(self):
        # 2,919 CAD at 0.00881 JPY->CAD is 331,328 JPY (rounded to whole yen)
        assert equivalent_milliunits(2919000, 0.00881, "JPY") == 331328000
        assert equivalent_milliunits(-61000, 0.00878, "JPY") == -6948000
        # two-decimal original currency keeps cents
        assert equivalent_milliunits(-15990, 1.0842, "EUR") == -14750


class TestBuildPreview:
    def make_rates(self):
        return RateTable({"2024-01-05": 0.0087987, "2024-01-08": 0.0088100})

    def test_skips_converted_zero_and_skipped_transactions(self):
        transactions = [
            {"id": "t1", "date": "2024-01-05", "amount": -1817000,
             "payee_name": "Ramen", "memo": None},
            {"id": "t2", "date": "2024-01-05", "amount": -5000000,
             "payee_name": "Hotel", "memo": "-5,000 JPY (FX rate: 0.0087987)"},
            {"id": "t3", "date": "2024-01-05", "amount": 0,
             "payee_name": "Starting balance", "memo": None},
            {"id": "t4", "date": "2024-01-05", "amount": -61000,
             "payee_name": "Reconciliation", "memo": "(skipped)"},
        ]
        rows = build_preview(transactions, self.make_rates(), "JPY", "USD")
        assert [r["id"] for r in rows] == ["t1"]
        assert rows[0]["new_milliunits"] == -15990
        assert rows[0]["new_memo"] == "-1,817 JPY (FX rate: 0.0087987)"

    def test_rows_carry_already_and_skip_alternatives(self):
        transactions = [
            {"id": "t1", "date": "2024-01-08", "amount": 2919000,
             "payee_name": "Transfer : BMO Chequing", "memo": None},
        ]
        rows = build_preview(transactions, self.make_rates(), "JPY", "USD")
        # 2,919 USD at 0.00881 is worth 331,328 JPY
        assert rows[0]["equivalent_display"] == "331,328 JPY"
        assert rows[0]["already_memo"] == "≈ 331,328 JPY (FX rate: 0.00881)"
        assert rows[0]["skip_memo"] == "(skipped)"
        assert is_converted(rows[0]["already_memo"])
        assert is_skipped(rows[0]["skip_memo"])

    def test_converted_display_uses_target_currency_decimals(self):
        transactions = [
            {"id": "t1", "date": "2024-01-05", "amount": -15990,
             "payee_name": "Ramen", "memo": None},
        ]
        # USD -> JPY budget: zero-decimal display, no trailing ".00"
        rows = build_preview(transactions, RateTable({"2024-01-05": 113.65}), "USD", "JPY")
        assert rows[0]["new_display"] == "-1,817"
        # JPY -> USD stays two-decimal
        rows = build_preview(transactions, RateTable({"2024-01-05": 0.0087987}), "JPY", "USD")
        assert rows[0]["new_display"] == "-0.14"

    def test_skips_split_transactions(self):
        split = {
            "id": "t1", "date": "2024-01-05", "amount": -3000000,
            "payee_name": "Combini", "memo": None,
            "subtransactions": [{"id": "s1", "amount": -1000000},
                                {"id": "s2", "amount": -2000000}],
        }
        plain = {"id": "t2", "date": "2024-01-05", "amount": -1817000,
                 "payee_name": "Ramen", "memo": None, "subtransactions": []}
        assert is_split(split)
        assert not is_split(plain)
        rows = build_preview([split, plain], self.make_rates(), "JPY", "USD")
        assert [r["id"] for r in rows] == ["t2"]

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
