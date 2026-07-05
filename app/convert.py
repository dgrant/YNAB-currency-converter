import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .rates import RateTable

# Matches the memo marker written by this app and by ynab.rmillan.com,
# e.g. "-1,817 JPY (FX rate: 0.0087987)". A transaction carrying this
# marker is considered already converted and is never converted again.
MARKER_RE = re.compile(r"\(FX rate: [0-9]*\.?[0-9]+\)")

# Memo marker for transactions the user chose to never convert (e.g. a
# reconciliation entry already recorded in the budget currency). Matches
# the marker rmillan's service appears to use for its skip feature.
SKIPPED_MARKER = "(skipped)"
SKIPPED_RE = re.compile(re.escape(SKIPPED_MARKER))

# ISO 4217 currencies with no minor unit (whole-number amounts).
ZERO_DECIMAL_CURRENCIES = {
    "JPY", "KRW", "VND", "ISK", "CLP", "PYG", "UGX", "RWF",
    "GNF", "XOF", "XAF", "KMF", "DJF", "VUV", "BIF",
}


def is_converted(memo: str | None) -> bool:
    return bool(memo and MARKER_RE.search(memo))


def is_skipped(memo: str | None) -> bool:
    return bool(memo and SKIPPED_RE.search(memo))


def is_split(txn: dict) -> bool:
    """YNAB split transactions: subtransactions must sum to the parent, so
    patching the parent's amount alone would be rejected or corrupt the split.
    Until subtransaction conversion is implemented, splits are skipped."""
    return bool(txn.get("subtransactions"))


def is_convertible(txn: dict) -> bool:
    """The single source of truth for which transactions get proposed:
    nonzero, not already converted, not skipped, and not a split."""
    memo = txn.get("memo")
    return (
        txn["amount"] != 0
        and not is_converted(memo)
        and not is_skipped(memo)
        and not is_split(txn)
    )


def decimal_digits(currency: str) -> int:
    return 0 if currency in ZERO_DECIMAL_CURRENCIES else 2


def format_amount(milliunits: int, currency: str) -> str:
    """Format a YNAB milliunit amount to the currency's minor unit, e.g. '-1,817' or '-45.30'."""
    digits = decimal_digits(currency)
    quantum = Decimal(1).scaleb(-digits)
    amount = (Decimal(milliunits) / 1000).quantize(quantum, rounding=ROUND_HALF_UP)
    return f"{amount:,}"


def format_original(milliunits: int, currency: str) -> str:
    """Format a YNAB milliunit amount as e.g. '-1,817 JPY' or '-45.30 EUR'."""
    return f"{format_amount(milliunits, currency)} {currency}"


def format_rate(rate: float) -> str:
    text = f"{rate:.7g}"
    if "e" in text or "E" in text:
        text = f"{rate:.10f}".rstrip("0")
    return text


def convert_milliunits(milliunits: int, rate: float, to_currency: str) -> int:
    """Convert an amount, rounding to the target currency's minor unit."""
    digits = decimal_digits(to_currency)
    quantum = Decimal(1).scaleb(-digits)
    converted = (Decimal(milliunits) / 1000 * Decimal(str(rate))).quantize(
        quantum, rounding=ROUND_HALF_UP
    )
    return int(converted * 1000)


def equivalent_milliunits(milliunits: int, rate: float, from_currency: str) -> int:
    """Inverse of convert_milliunits: what a budget-currency amount is worth in
    the original currency (for 'already in budget currency' memos)."""
    digits = decimal_digits(from_currency)
    quantum = Decimal(1).scaleb(-digits)
    equivalent = (Decimal(milliunits) / 1000 / Decimal(str(rate))).quantize(
        quantum, rounding=ROUND_HALF_UP
    )
    return int(equivalent * 1000)


def build_marker(milliunits: int, from_currency: str, rate: float) -> str:
    return f"{format_original(milliunits, from_currency)} (FX rate: {format_rate(rate)})"


def _append_marker(old_memo: str | None, marker: str) -> str:
    return f"{old_memo} {marker}" if old_memo else marker


def build_memo(old_memo: str | None, milliunits: int, from_currency: str, rate: float) -> str:
    return _append_marker(old_memo, build_marker(milliunits, from_currency, rate))


def build_already_memo(
    old_memo: str | None, equivalent: int, from_currency: str, rate: float
) -> str:
    """Memo for a transaction already entered in the budget currency: record its
    original-currency equivalent. The '≈' distinguishes it from a real conversion,
    while the '(FX rate: …)' marker still excludes it from future previews."""
    return _append_marker(old_memo, f"≈ {build_marker(equivalent, from_currency, rate)}")


def build_skip_memo(old_memo: str | None) -> str:
    return _append_marker(old_memo, SKIPPED_MARKER)


def build_preview(
    transactions: list[dict],
    rates: RateTable,
    from_currency: str,
    to_currency: str,
) -> list[dict]:
    """Compute the proposed conversion for each not-yet-converted transaction."""
    rows = []
    for txn in transactions:
        if not is_convertible(txn):
            continue
        rate = rates.rate_for(date.fromisoformat(txn["date"]))
        new_milliunits = convert_milliunits(txn["amount"], rate, to_currency)
        # For the "already in budget currency" action: the amount stays as-is,
        # so its original-currency equivalent goes in the memo instead.
        equivalent = equivalent_milliunits(txn["amount"], rate, from_currency)
        rows.append(
            {
                "id": txn["id"],
                "date": txn["date"],
                "payee_name": txn.get("payee_name") or "",
                "old_memo": txn.get("memo") or "",
                "original_milliunits": txn["amount"],
                "original_display": format_original(txn["amount"], from_currency),
                "rate": rate,
                "rate_display": format_rate(rate),
                "new_milliunits": new_milliunits,
                "new_display": format_amount(new_milliunits, to_currency),
                "new_memo": build_memo(txn.get("memo"), txn["amount"], from_currency, rate),
                "equivalent_display": format_original(equivalent, from_currency),
                "already_memo": build_already_memo(
                    txn.get("memo"), equivalent, from_currency, rate
                ),
                "skip_memo": build_skip_memo(txn.get("memo")),
            }
        )
    return rows
