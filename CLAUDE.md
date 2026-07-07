# CLAUDE.md — notes for future agents

Self-hosted clone of the "Multi-currency for YNAB" conversions page
(ynab.rmillan.com/conversions). Multi-user web app: enter transactions in a
YNAB account in their original foreign currency, and this app converts each to
the budget currency using the exchange rate of the transaction's date, editing
the amount in place in YNAB and annotating the memo.

## Stack & layout

- **Python + FastAPI + Jinja2**, server-rendered. `httpx` for HTTP.
- **SQLite** (`data/app.db`, stdlib sqlite3, no ORM): users, per-user YNAB
  connections, per-user conversions. No transaction data is stored — YNAB
  itself is the source of truth for what's already been converted (see the
  memo marker below).
- FX rates from **Frankfurter** (free ECB API, no key, historical by date).

```
app/
  main.py            # app factory, SessionMiddleware, db.init, route wiring
  config.py          # env: SECRET_KEY, YNAB_CLIENT_ID/SECRET, PUBLIC_BASE_URL, DATA_DIR
  db.py              # SQLite schema + connection helper (per-op connections, WAL)
  users.py           # User + UserStore, scrypt password hashing (stdlib)
  auth.py            # signup/login/logout, per-email throttle, require_login -> User
  connections.py     # ConnectionStore: per-user YNAB OAuth token pair
  oauth.py           # YNAB OAuth: authorize URL, code exchange, refresh, get_access_token
  store.py           # ConversionStore: per-user conversion configs (SQLite)
  ynab.py            # YNABClient: budgets, accounts, transactions, bulk PATCH
                     #   (one pooled httpx client; per-user token as request header)
  rates.py           # FrankfurterClient + RateTable (business-day fallback)
  convert.py         # core: filter unconverted, compute amounts/memos
  import_legacy.py   # one-shot v1 migration: python -m app.import_legacy <email>
  routes/conversions.py  # list / new / edit / delete / bulk-delete / detail / preview / apply
                         #   (all scoped by user)
  routes/settings.py     # /settings: OAuth start/callback, disconnect
  templates/ static/
tests/               # pytest (respx-mocked YNAB + Frankfurter); test_app_flow.py is the full HTTP flow
```

## Key conventions (don't break these)

- **YNAB amounts are milliunits** (integer, e.g. -1817000 = -1,817). Do all
  money math in integers; round to the target currency's minor unit
  (`convert.py: convert_milliunits`, `ZERO_DECIMAL_CURRENCIES` for JPY etc.).
- **Memo marker** — after converting, the memo gets
  `-1,817 JPY (FX rate: 0.0087987)` appended (matches ynab.rmillan.com exactly).
  "Already converted" is detected *only* by `MARKER_RE` (`\(FX rate: …\)`) in
  `convert.py`. This is how converting one transaction now and others later
  works, and how transactions previously converted by rmillan's service are
  skipped. Keep the format compatible.
- **Per-row preview actions** — besides Convert, a row can be marked
  "already in the budget currency": *Already \<CUR\> (memo …)* keeps the amount
  and appends `≈ 331,754 JPY (FX rate: 0.0087987)` (the `≈` marks it as an
  equivalence note, not a conversion — the `(FX rate: …)` part still matches
  `MARKER_RE` so it's excluded from future previews), and *mark skipped*
  keeps the amount and appends `(skipped)` (`SKIPPED_RE`, also excluded).
  Both actions PATCH the memo only, never the amount.
- **Preview → approve** — `preview.html` renders proposed changes with the new
  amount/memo in hidden form fields; `apply` writes exactly those. No
  server-side pending state, so approve reflects what was shown even if rates
  move afterward.
- **Auth & multi-user** — email+password accounts (open signup), scrypt
  hashes, session stores `user_id`; `require_login` is a dependency returning
  the `User` and every store call is scoped by `user.id` — never query
  conversions or connections without it. `auth.py` remains the swap point for
  Google Sign-In later (an OIDC flow would set the same `user_id` session
  key). `/login` is brute-force throttled per email (in-memory, module state).
- **Per-user YNAB credentials** — each user connects on `/settings` via OAuth
  ("Connect to YNAB", available only when `YNAB_CLIENT_ID/SECRET` are set).
  OAuth is the only connection type — the personal-access-token path was
  removed for YNAB OAuth-App-Review compliance (2026-07). `get_access_token`
  transparently refreshes expired OAuth tokens; a *rejected* refresh (revoked
  grant) deletes the connection, transient failures don't. Any legacy `pat`
  row (from before removal) has no refresh token, so it's deleted on next
  access and the user re-connects via OAuth. Routes that need YNAB use the
  `require_ynab` dependency, which 303s to `/settings` when unconnected.
- **CSRF** — every POST form must include `{{ csrf_input(request) }}`
  (template global in `templates.py`); `verify_csrf` is a dependency on both
  routers and 403s POSTs without the session's token. Remember this when
  adding any new form or POST route (tests fetch the token from the login
  page — see `get_csrf` in `test_app_flow.py`).
- **Split transactions are skipped** — `is_split()` in `convert.py`; never
  let apply patch a split parent's top-level amount.
- **Upstream errors** — raise `YNABError`/`RatesError`; exception handlers in
  `main.py` render `error.html` (429 gets its own copy). A `YNABError` with
  `status_code == 401` (YNAB's documented signal for an invalid/expired/
  revoked access token) instead 303s to `/settings?error=revoked` — no error
  page, since the fix is to reconnect, not retry. Idempotent GETs go through
  `app/http.py: get_with_retry`; the PATCH is never retried.
- **`last_synced`** (`store.mark_synced`) must be written only after the
  operation it certifies has actually succeeded — after `build_preview` in
  `preview()`, after `update_transactions` (or the "nothing to send" branch)
  in `apply()`. Marking it earlier (e.g. right after the initial
  `get_transactions` fetch) means a later failure — a bad FX rate, a rejected
  PATCH — leaves the UI claiming "synced" for a cycle that never completed.
- **Schema changes need a migration, not just a `SCHEMA` edit** — `CREATE
  TABLE IF NOT EXISTS` in `db.py` never touches an already-existing table, so
  adding/changing a column only takes effect on a fresh `data/app.db`. The
  live deployment's DB is not fresh. Add an idempotent entry to `db.py`'s
  `_MIGRATIONS` tuple (`(table, column, definition)`); `_apply_migrations`
  ALTERs it in on every `init()` if the column is missing. See `last_synced`
  for the pattern, and `tests/test_db.py` for how to test it against a
  pre-migration schema (a fresh tmp_path DB never exercises the ALTER
  branch, since `CREATE TABLE IF NOT EXISTS` already includes the column).

## Dev

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/mypy   # CI runs these too — keep them green
# run locally (SECRET_KEY is the only required env var):
SECRET_KEY=y .venv/bin/uvicorn app.main:app --port 8000
```

`YNAB_API_BASE` / `FRANKFURTER_API_BASE` env vars let tests point at mock
servers (see `tests/` and `test_app_flow.py`).

## Deploy

Live at **https://fxforynab.davidgrant.ca**. Verify from outside with
`curl -s https://fxforynab.davidgrant.ca/healthz` → `{"status":"ok","version":"<VERSION file contents>"}`
(plain GET — agent sandbox proxies 405 HEAD requests). `version` is the
release version (root `VERSION` file, gstack's `MAJOR.MINOR.PATCH.MICRO`),
copied into the image at build time and read directly by the app
(`app/config.py`); the page footer shows it too. It only changes when
`/ship` bumps `VERSION`, so it answers "what release is live," not "what
commit" — for that, `autodeploy.sh` checks the running container's
`git_sha` image label (`docker inspect`) against the commit it just
deployed, since `GIT_SHA` (baked in via `ARG GIT_SHA` and exported as
`BUILD_ID`/the label) is the only value guaranteed unique per commit.
`BUILD_ID` also drives static-asset cache-busting for the same reason.

Docker Compose; the container binds to **127.0.0.1:8000** (not public).
**Single uvicorn worker (no `--workers`) is a correctness assumption, not just a
resource choice:** the per-conversion apply lock (`_apply_locks` in
`routes/conversions.py`) and the OAuth token-refresh locks (`_refresh_locks` in
`oauth.py`) are in-process; adding workers would break the double-submit and
refresh-rotation serialization they provide. Don't add `--workers` without moving
that coordination to a shared store.
Server: David's Linode (Debian 12), app at `~/YNAB-currency-converter`,
fronted by host nginx (vhost `/etc/nginx/sites-available/fxforynab.davidgrant.ca`
→ `proxy_pass http://127.0.0.1:8000`) with a certbot Let's Encrypt cert
(auto-renew via timer). `docker compose` works without sudo; sudo needs a
password. Secrets (`SECRET_KEY`, optional `YNAB_CLIENT_SECRET`, legacy
`APP_PASSWORD`/`YNAB_TOKEN`) live only in `.env` on the server, set by
David — never ask for or print them. See `.env.example` and `DEPLOY.md`;
migrating the v1 single-user data is `python -m app.import_legacy <email>`
(documented there).

**Auto-deploy:** merging/pushing to the default branch (`master`) IS the
deploy process. CI (`.github/workflows/ci.yml`) runs pytest; a cron job in
user david's crontab (every 2 min, flock-guarded) runs `deploy/autodeploy.sh`,
which fast-forwards and rebuilds only when new default-branch commits have
green checks (no secrets — the server polls public GitHub over HTTPS), then
health-checks `/healthz` and verifies the running container's `git_sha`
label matches the deployed commit. Latency ~2 min + build. Logs: `~/autodeploy.log`;
last deployed SHA: `~/YNAB-currency-converter/.last-deployed`. Pause by
commenting out the crontab line. Manual fallback only:
`git pull && docker compose up -d --build`.

**Agents cannot SSH to the server** — the sandbox egress proxy relays TLS
only (it also blocks `api.github.com`; use the GitHub MCP tools). All server
work is guided-manual: give David short copy-paste commands for his Linode
Lish web console and have him paste back output. He's often on a phone —
keep commands short and output minimal.

## Future work

See `TODOS.md` — the maintained backlog (features, known bugs like split
transactions, ops). Bigger arcs: Google Sign-In, auto-sync scheduler,
crypto. (Multi-user + YNAB OAuth landed 2026-07.)

## gstack (REQUIRED — global install)

David requires gstack for this repo; see the install steps below.

**Before doing ANY work, verify gstack is installed:**

```bash
test -d ~/.claude/skills/gstack/bin && echo "GSTACK_OK" || echo "GSTACK_MISSING"
```

If GSTACK_MISSING: STOP. Do not proceed. Tell the user:

> gstack is required for all AI-assisted work in this repo.
> Install it:
> ```bash
> git clone --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
> cd ~/.claude/skills/gstack && ./setup --team
> ```
> Then restart your AI coding tool.

Do not skip skills, ignore gstack errors, or work around missing gstack.

Using gstack skills: After install, skills like /qa, /ship, /review, /investigate,
and /browse are available. Use /browse for all web browsing.
Use ~/.claude/skills/gstack/... for gstack file paths (the global path).
