"""The account-name currency guess is implemented twice — _guess_currency in
app/routes/conversions.py (the batch form) and app/static/currency_guess.js
(the single new/edit form). This runs the SAME fixture through both so a change
to one that diverges from the other fails here."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from currency_guess_cases import CURRENCY_CODES, GUESS_CASES

from app.routes.conversions import _guess_currency

_JS_FILE = Path(__file__).resolve().parent.parent / "app" / "static" / "currency_guess.js"
_NODE = shutil.which("node")


def test_python_guess_matches_fixture():
    codes = set(CURRENCY_CODES)
    for account_name, expected in GUESS_CASES:
        assert _guess_currency(account_name, codes) == expected, account_name


@pytest.mark.skipif(_NODE is None, reason="node not available to run the JS twin")
def test_js_guess_matches_python():
    """Execute the real app/static/currency_guess.js over the same fixture and
    assert it agrees with the Python implementation on every case."""
    names = [name for name, _ in GUESS_CASES]
    driver = (
        f"const {{ guessCurrency }} = require({json.dumps(str(_JS_FILE))});\n"
        f"const codes = new Set({json.dumps(CURRENCY_CODES)});\n"
        f"const names = {json.dumps(names)};\n"
        "console.log(JSON.stringify(names.map((n) => guessCurrency(n, codes))));\n"
    )
    proc = subprocess.run(
        [_NODE, "-e", driver], capture_output=True, text=True, check=True
    )
    js_results = json.loads(proc.stdout)
    for (account_name, expected), got in zip(GUESS_CASES, js_results, strict=True):
        assert got == expected, f"JS guess for {account_name!r}: {got!r} != {expected!r}"
