# Currency Converter for YNAB

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

1. **Create a conversion** — pick a budget, the foreign-currency account, and
   a start date (your earliest unconverted transaction). The original currency
   is guessed from the account name, and the currency to convert *into* comes
   straight from your plan in YNAB. Setting up several accounts? **Batch add**
   lists every account you haven't configured yet and creates them all at once.
2. **Preview** — fetches the account's transactions from YNAB since the
   start date, skips any whose memo already carries an `(FX rate: …)` marker,
   and shows a table of proposed conversions (date-accurate rates from the
   free [Frankfurter](https://frankfurter.dev) API, ECB data). **Preview all**
   does this for every account at once and shows one combined page grouped by
   account, so the daily loop across several accounts stays two clicks.
3. **Approve** — untick anything you don't want, approve, and the selected
   transactions are updated in YNAB via the official API. Afterwards the
   conversion's start date moves forward past everything handled, so future
   previews stay fast as the account's history grows.

The conversions page doubles as a dashboard: each account shows a pending
count so you can see at a glance which ones have new transactions to convert.
The count updates whenever you preview, and an optional setting (off by
default) refreshes stale counts automatically when you open the page.

Nothing is written to YNAB without your approval. The app stores no
transaction data — just accounts (email + password), each user's YNAB
credentials, their configured conversions, and a small activity log (logins,
conversions applied) in a SQLite database (`data/app.db`). YNAB itself is the
source of truth for what's been converted.

## Multi-user

Anyone can sign up with email + password. Each user connects their own YNAB
account via OAuth: register a YNAB OAuth application (free, app.ynab.com →
Developer Settings), set `YNAB_CLIENT_ID` / `YNAB_CLIENT_SECRET`, and users
get a "Connect to YNAB" button — no API key needed, revocable from YNAB at
any time. Tokens refresh automatically.

Deleting an account is self-serve: **Settings → Delete account** asks for the
password (a logged-in session alone can't destroy an account), then removes the
account, its YNAB connection, every configured conversion and its activity
history. The freed space is overwritten rather than left readable
(`PRAGMA secure_delete`), though under concurrent reads a copy can linger in
the write-ahead log until the next checkpoint clears it —
`db.checkpoint_wal` retries, then logs at ERROR rather than failing the
request. Nothing in YNAB changes — transactions already converted keep their
amounts and memos — and the OAuth grant itself is revoked by the user from
YNAB → Account Settings → Security. To action a request for someone who can no
longer sign in, run it from the server:

```bash
docker compose exec app python -m app.delete_user them@example.com
```

See [DEPLOY.md](DEPLOY.md#deleting-a-user-account) for the details, including
the one-off compaction step worth running after upgrading to v0.6.0.0.

## Running

```bash
cp .env.example .env   # then edit: SECRET_KEY (and optionally the OAuth vars)
docker compose up -d   # http://localhost:8000
```

Or without Docker (needs [uv](https://docs.astral.sh/uv/)):

```bash
uv sync --no-dev
set -a; source .env; set +a
uv run uvicorn app.main:app --port 8000
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
uv sync   # installs runtime + dev dependencies
uv run pytest
```

Release history is in [CHANGELOG.md](CHANGELOG.md); the backlog is
[TODOS.md](TODOS.md).

## Notes & limitations

- Email + password sign-in (`app/auth.py`); Google Sign-In is a possible
  later swap in the same file.
- Manual sync only — no scheduler.
- Fiat currencies supported by Frankfurter (~30 major ones); no crypto.
- YNAB amounts are milliunits; conversions round to the target currency's
  minor unit (respecting zero-decimal currencies like JPY).
