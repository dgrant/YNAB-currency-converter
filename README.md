# YNAB Currency Converter

A self-hosted clone of the "Multi-currency for YNAB" conversions page
(ynab.rmillan.com/conversions).

Enter transactions in a YNAB account in their **original foreign currency**;
this app converts each one to your budget's currency using the exchange rate
of the transaction's date, and updates it in YNAB — editing the amount in
place and appending a memo like:

```
-1,817 JPY (FX rate: 0.0087987)
```

That memo format matches the one used by ynab.rmillan.com, so transactions
already converted by that service are recognized and never converted again.

## How it works

1. **Create a conversion** — pick a budget, the foreign-currency account, the
   original currency, and a start date (your earliest unconverted transaction).
2. **Preview sync** — fetches the account's transactions from YNAB since the
   start date, skips any whose memo already carries an `(FX rate: …)` marker,
   and shows a table of proposed conversions (date-accurate rates from the
   free [Frankfurter](https://frankfurter.dev) API, ECB data).
3. **Approve** — untick anything you don't want, approve, and the selected
   transactions are updated in YNAB via the official API in one bulk call.

Nothing is written to YNAB without your approval. The only state the app
keeps is `data/conversions.json` (the list of configured conversions) —
YNAB itself is the source of truth for what's been converted.

## Running

```bash
cp .env.example .env   # then edit: APP_PASSWORD, SECRET_KEY, YNAB_TOKEN
docker compose up -d   # http://localhost:8000
```

Or without Docker:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
set -a; source .env; set +a
.venv/bin/uvicorn app.main:app --port 8000
```

See [DEPLOY.md](DEPLOY.md) for VPS deployment.

## Configuration

| Variable       | Purpose                                                        |
| -------------- | -------------------------------------------------------------- |
| `APP_PASSWORD` | Password for the web UI (single-user)                           |
| `SECRET_KEY`   | Session-cookie signing key (`python3 -c "import secrets; print(secrets.token_hex(32))"`) |
| `YNAB_TOKEN`   | YNAB personal access token (app.ynab.com → Developer Settings) |
| `DATA_DIR`     | Directory for `conversions.json` (default `data`)              |

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

## Notes & limitations (v1)

- Single user, single password. Auth lives in `app/auth.py` behind one
  session key, so swapping in Google Sign-In later only touches that file.
- Manual sync only — no scheduler.
- Fiat currencies supported by Frankfurter (~30 major ones); no crypto.
- Uses a YNAB personal access token, not OAuth.
- YNAB amounts are milliunits; conversions round to the target currency's
  minor unit (respecting zero-decimal currencies like JPY).
