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
  connections.py     # ConnectionStore: per-user YNAB credentials (PAT or OAuth pair)
  oauth.py           # YNAB OAuth: authorize URL, code exchange, refresh, get_access_token
  store.py           # ConversionStore: per-user conversion configs (SQLite)
  ynab.py            # YNABClient: budgets, accounts, transactions, bulk PATCH
                     #   (one pooled httpx client; per-user token as request header)
  rates.py           # FrankfurterClient + RateTable (business-day fallback)
  convert.py         # core: filter unconverted, compute amounts/memos
  import_legacy.py   # one-shot v1 migration: python -m app.import_legacy <email>
  routes/conversions.py  # list / new / detail / preview / apply (all scoped by user)
  routes/settings.py     # /settings: PAT entry, OAuth start/callback, disconnect
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
- **Per-user YNAB credentials** — each user connects on `/settings`: OAuth
  ("Connect to YNAB", only when `YNAB_CLIENT_ID/SECRET` are set) or a pasted
  personal access token. `oauth.get_access_token` transparently refreshes
  expired OAuth tokens; a *rejected* refresh (revoked grant) deletes the
  connection, transient failures don't. Routes that need YNAB use the
  `require_ynab` dependency, which 303s to `/settings` when unconnected.
- **CSRF** — every POST form must include `{{ csrf_input(request) }}`
  (template global in `templates.py`); `verify_csrf` is a dependency on both
  routers and 403s POSTs without the session's token. Remember this when
  adding any new form or POST route (tests fetch the token from the login
  page — see `get_csrf` in `test_app_flow.py`).
- **Split transactions are skipped** — `is_split()` in `convert.py`; never
  let apply patch a split parent's top-level amount.
- **Upstream errors** — raise `YNABError`/`RatesError`; exception handlers in
  `main.py` render `error.html` (429 gets its own copy). Idempotent GETs go
  through `app/http.py: get_with_retry`; the PATCH is never retried.

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

Live at **https://ynabfx.davidgrant.ca**. Verify from outside with
`curl -s https://ynabfx.davidgrant.ca/healthz` → `{"status":"ok","version":"<git sha>"}`
(plain GET — agent sandbox proxies 405 HEAD requests). The version is baked
in at image build time (`ARG GIT_SHA`, exported by `autodeploy.sh`), so this
answers "what's live" without SSH; the page footer shows it too.

Docker Compose; the container binds to **127.0.0.1:8000** (not public).
Server: David's Linode (Debian 12), app at `~/YNAB-currency-converter`,
fronted by host nginx (vhost `/etc/nginx/sites-available/ynabfx.davidgrant.ca`
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
health-checks `/healthz` and verifies the reported version matches the
deployed SHA. Latency ~2 min + build. Logs: `~/autodeploy.log`;
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
