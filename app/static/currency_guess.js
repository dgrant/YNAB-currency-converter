// Guess a transaction's original currency from the account name: if a whole
// word in the name is a known currency code ("Chequing USD" -> USD), return it,
// else "". This is the JS twin of _guess_currency in app/routes/conversions.py
// (used by the batch form) — the two MUST agree. tests/test_currency_guess.py
// runs both over the shared fixture in tests/currency_guess_cases.py to keep
// them from silently diverging.
function guessCurrency(accountName, currencyCodes) {
  return accountName.toUpperCase().split(/[^A-Z]+/).find((w) => currencyCodes.has(w)) || "";
}

// Usable both as a browser global (conversion_form.html loads it via <script
// src>) and as a Node module (the cross-language test requires it).
if (typeof module !== "undefined" && module.exports) {
  module.exports = { guessCurrency };
}
