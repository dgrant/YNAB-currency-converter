# CLAUDE.md — notes for future agents

Self-hosted clone of the "Multi-currency for YNAB" conversions page
(ynab.rmillan.com/conversions). Single-user web app: enter transactions in a
YNAB account in their original foreign currency, and this app converts each to
the budget currency using the exchange rate of the transaction's date, editing
the amount in place in YNAB and annotating the memo.

## Stack & layout

- **Python + FastAPI + Jinja2**, server-rendered. `httpx` for HTTP.
- **No database.** The only persisted state is `data/conversions.json` (list of
  configured conversions). YNAB itself is the source of truth for what's already
  been converted — see the memo marker below.
- FX rates from **Frankfurter** (free ECB API, no key, historical by date).

```
app/
  main.py            # app factory, SessionMiddleware, route wiring
  config.py          # env: APP_PASSWORD, SECRET_KEY, YNAB_TOKEN, DATA_DIR
  auth.py            # single-password login + require_login dep (swap point for Google SSO)
  store.py           # ConversionStore: load/save data/conversions.json (atomic write)
  ynab.py            # YNABClient: budgets, accounts, transactions, bulk PATCH
  rates.py           # FrankfurterClient + RateTable (business-day fallback)
  convert.py         # core: filter unconverted, compute amounts/memos
  routes/conversions.py  # list / new / detail / preview / apply
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
- **Auth** — one `APP_PASSWORD` behind a session cookie; all of `auth.py` is the
  intended swap point for Google Sign-In later (keep the `authed` session key).

## Dev

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
# run locally (needs the env vars):
APP_PASSWORD=x SECRET_KEY=y YNAB_TOKEN=... .venv/bin/uvicorn app.main:app --port 8000
```

`YNAB_API_BASE` / `FRANKFURTER_API_BASE` env vars let tests point at mock
servers (see `tests/` and `test_app_flow.py`).

## Deploy

Live at **https://ynabfx.davidgrant.ca**. Verify from outside with
`curl -s -o /dev/null -w "%{http_code}" https://ynabfx.davidgrant.ca/login`
→ 200 (plain GET — agent sandbox proxies 405 HEAD requests).

Docker Compose; the container binds to **127.0.0.1:8000** (not public).
Server: David's Linode (Debian 12), app at `~/YNAB-currency-converter`,
fronted by host nginx (vhost `/etc/nginx/sites-available/ynabfx.davidgrant.ca`
→ `proxy_pass http://127.0.0.1:8000`) with a certbot Let's Encrypt cert
(auto-renew via timer). `docker compose` works without sudo; sudo needs a
password. Secrets (`APP_PASSWORD`, `SECRET_KEY`, `YNAB_TOKEN`) live only in
`.env` on the server, set by David — never ask for or print them. See
`.env.example` and `DEPLOY.md`.

**Auto-deploy:** merging/pushing to the default branch (`master`) IS the
deploy process. CI (`.github/workflows/ci.yml`) runs pytest; a cron job in
user david's crontab (every 2 min, flock-guarded) runs `deploy/autodeploy.sh`,
which fast-forwards and rebuilds only when new default-branch commits have
green checks (no secrets — the server polls public GitHub over HTTPS), then
health-checks `/login`. Latency ~2 min + build. Logs: `~/autodeploy.log`;
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
transactions, ops). Bigger arcs: multi-user, Google Sign-In, auto-sync
scheduler, crypto, YNAB OAuth (currently a personal access token).
