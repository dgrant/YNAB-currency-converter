"""Canonical account-name -> guessed-currency cases, run through BOTH the
Python (_guess_currency) and JavaScript (app/static/currency_guess.js)
implementations by tests/test_currency_guess.py so the two can't diverge."""

# A representative currency-code universe for the cases below.
CURRENCY_CODES = ["USD", "EUR", "JPY", "GBP", "CAD", "AUD", "CHF"]

# (account_name, expected_code) — "" means no confident guess.
GUESS_CASES = [
    ("Chequing USD", "USD"),
    ("USD Cash", "USD"),
    ("Japan Trip JPY", "JPY"),
    ("Euro Account (EUR)", "EUR"),       # EURO is not a code; EUR at the end is
    ("Rainy Day", ""),                   # no code anywhere
    ("usd lowercase", "USD"),            # case-insensitive
    ("GBP-Savings", "GBP"),              # non-alpha separator splits words
    ("MyUSDaccount", ""),                # embedded, not a whole word -> no match
    ("CAD/USD", "CAD"),                  # first matching word wins
    ("account123", ""),                  # digits split, no code word
    ("CHF", "CHF"),                      # the whole name is a code
    ("Travel — AUD", "AUD"),             # unicode punctuation as separator
]
