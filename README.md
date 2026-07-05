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

Nothing is written to YNAB without your approval. The app stores no
transaction data — just accounts (email + password), each user's YNAB
credentials, and their configured conversions in a small SQLite database
(`data/app.db`). YNAB itself is the source of truth for what's been
converted.

## Multi-user

Anyone can sign up with email + password. Each user connects their own YNAB
account, either:

- **OAuth** (recommended): if you register a YNAB OAuth application (free,
  app.ynab.com → Developer Settings) and set `YNAB_CLIENT_ID` /
  `YNAB_CLIENT_SECRET`, users get a "Connect to YNAB" button — no API key
  needed, revocable from YNAB at any time. Tokens refresh automatically.
- **Personal access token**: always available as a fallback — paste a token
  from YNAB's Developer Settings on the Settings page.

## Running

```bash
cp .env.example .env   # then edit: SECRET_KEY (and optionally the OAuth vars)
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

| Variable             | Purpose                                                        |
| -------------------- | -------------------------------------------------------------- |
| `SECRET_KEY`         | Session-cookie signing key (`python3 -c "import secrets; print(secrets.token_hex(32))"`) |
| `YNAB_CLIENT_ID`     | Optional: YNAB OAuth app id — enables "Connect to YNAB"        |
| `YNAB_CLIENT_SECRET` | Optional: YNAB OAuth app secret                                |
| `PUBLIC_BASE_URL`    | Optional: public origin for the OAuth redirect URI behind a proxy |
| `DATA_DIR`           | Directory for `app.db` (default `data`)                        |
| `APP_PASSWORD` / `YNAB_TOKEN` | Legacy v1 vars, read only by `python -m app.import_legacy` |

### Migrating from v1 (single-user)

With the old `.env` still in place, create your account from the legacy
config — it becomes a user whose password is the old `APP_PASSWORD`, keeps
the old `YNAB_TOKEN` as its connection, and imports `conversions.json`
(ids preserved):

```bash
docker compose exec app python -m app.import_legacy you@example.com
```

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

## Notes & limitations

- Email + password sign-in (`app/auth.py`); Google Sign-In is a possible
  later swap in the same file.
- Manual sync only — no scheduler.
- Fiat currencies supported by Frankfurter (~30 major ones); no crypto.
- YNAB amounts are milliunits; conversions round to the target currency's
  minor unit (respecting zero-decimal currencies like JPY).
