# TODOS

Organized by component, P0 (blocking) → P4 (someday), completed items at the
bottom. v1 is live and works; nothing here is required for day-to-day use.
See CLAUDE.md for conventions that must not break (memo marker format,
milliunit math, preview→approve contract). This repo versions by git SHA
(see `/healthz`), so completed items are annotated with dates, not versions.

## Conversions

### Daily auto-sync scheduler

**What:** A background job that runs preview for every conversion and either auto-applies or records what's pending.

**Why:** Removes the manual "open the app and press preview" step — conversions happen (or queue up) on their own.

**Context:** In-process `asyncio` task or a second container on cron. Needs YNAB error/429 handling first, and a decision on auto-apply vs. notify-only (start with notify-only — the preview→approve contract is a safety feature). Going async on the HTTP clients remains a scheduler-time decision (both are pooled sync singletons today). The scheduler must not quietly turn into server-side pending state — keep the stateless preview→approve design (see reference notes below).

**Effort:** L
**Priority:** P2
**Depends on:** YNAB delta requests (below) help; notify-only decision

### Notifications for pending conversions

**What:** When unconverted transactions appear, send an email / ntfy.sh / Telegram ping with the count and a link.

**Why:** Makes the app pull-free: enter transactions on the phone, get pinged, tap approve.

**Context:** Pairs with the scheduler — it supplies the "unconverted transactions appeared" signal.

**Effort:** M
**Priority:** P2
**Depends on:** Daily auto-sync scheduler

### Undo / revert a conversion

**What:** Per-transaction and whole-batch undo on the applied page.

**Why:** A mis-approved conversion currently requires manual repair in YNAB.

**Context:** The memo marker contains everything needed to reverse an apply: original amount and rate. Parse `-1,817 JPY (FX rate: 0.0087987)` back out, restore the original milliunits, strip the marker from the memo. **Must exclude `≈ …` equivalence markers** (the "already in budget currency" action): those amounts were never changed, so "restoring" the parsed value would corrupt them.

**Effort:** M
**Priority:** P2
**Depends on:** None

### Verify YNAB API behaviors against a live budget

**What:** One-time manual checks of two behaviors this app now relies on but that only exist against mocks: (a) a memo-only PATCH (`id` + `memo`, no `amount`) leaves the amount unchanged; (b) whether YNAB's 500 memo cap counts characters or bytes.

**Why:** The "already in budget currency" / "skip" actions send memo-only PATCHes, and the marker-truncation math budgets 500 UTF-8 bytes; docs say the right things but neither has been observed against the real API.

**Context:** Surfaced by the 2026-07-05 pre-landing review. Check the first real "Already \<CUR\>" transaction after deploy: amount unchanged, memo intact including the closing `)` of `(FX rate: …)`. The truncation is already byte-safe (≤500 bytes AND chars), so (b) is belt-and-braces.

**Effort:** S
**Priority:** P2
**Depends on:** This branch deployed

### Convert all accounts at once

**What:** A "Preview all" on the index page that runs the preview for every configured conversion and shows one combined, grouped table with a single approve.

**Why:** Today preview/apply is per conversion; multiple foreign-currency accounts mean repeated round-trips.

**Context:** Rate fetches stay per conversion (different currency pairs), but YNAB updates can share one bulk PATCH per budget (`update_transactions` already takes a list); keep per-row unticking, and keep the preview→approve hidden-field contract. This is also most of the groundwork for the scheduler item.

**Effort:** M
**Priority:** P2
**Depends on:** None

### Convert split transactions properly

**What:** Convert each subtransaction with the same rate, rounding such that they still sum to the converted parent, then PATCH parent + subtransactions together.

**Why:** Splits are currently skipped with a note — the one transaction type the app can't handle.

**Context:** The dangerous half is fixed: splits (non-empty `subtransactions`) are detected, skipped in `build_preview`, counted with a note in the preview, and re-checked at apply time, so apply can no longer corrupt them. This item is the remaining feature work.

**Effort:** M
**Priority:** P2
**Depends on:** None

### Verify rmillan's `(skipped)` byte format

**What:** Confirm the exact memo string rmillan's service writes for skipped transactions.

**Why:** Our `(skipped)` marker (now case-insensitive) is byte-compatible with a guess; if their format differs, transactions skipped on their site re-appear here (and vice versa).

**Context:** Create a throwaway rmillan account (see reference notes below), connect a test YNAB budget, skip a transaction there, and inspect the memo it writes.

**Effort:** S
**Priority:** P3
**Depends on:** None

### YNAB delta requests

**What:** Use `last_knowledge_of_server` on transaction fetches to cut request volume.

**Why:** The ~200 req/hour YNAB budget matters once the scheduler / pending-count badges land.

**Context:** Extracted from the completed 429-handling item ("still worth adding").

**Effort:** S
**Priority:** P3
**Depends on:** Worth doing alongside the scheduler

### Auto-advance `start_date` after apply

**What:** After a successful apply, bump the stored `start_date` to the oldest still-unconverted date (or keep a `last_synced` field).

**Why:** Every preview refetches all transactions since the original start date and re-skips converted ones — fetches grow with history.

**Effort:** S
**Priority:** P3
**Depends on:** None

### Show pending counts on the index page

**What:** A "N unconverted" badge per conversion row, fetched on demand or by the scheduler.

**Why:** Turns the static conversions list into a dashboard.

**Effort:** M
**Priority:** P3
**Depends on:** None

### Manual rate override in preview

**What:** Editable rate per row (recompute amount client-side or on re-preview).

**Why:** Cash exchanged at a non-market rate, card FX fees, etc. don't match the ECB rate.

**Effort:** M
**Priority:** P3
**Depends on:** None

### Crypto / non-ECB currencies

**What:** Add a second rate source behind the `RateTable` interface (e.g. CoinGecko for crypto) and pick per conversion.

**Why:** Frankfurter is ~30 fiat currencies (ECB data).

**Effort:** M
**Priority:** P4
**Depends on:** None

## Auth & Accounts

### YNAB OAuth

**What:** Standard authorization-code flow instead of a personal access token.

**Why:** Prerequisite for multi-user; also removes the long-lived token from `.env`.

**Context:** Confirmed how ynab.rmillan.com does it (2026-07): a "Connect to YNAB" button linking to `https://app.youneedabudget.com/oauth/authorize?client_id=…&response_type=code&redirect_uri=https://ynab.rmillan.com/oauth`. End users need no API key; they just click Authorize in YNAB. The app owner registers an OAuth application once (free, YNAB Developer Settings) to get the client id/secret and set the redirect URI. Implementation here: `/oauth/start` + `/oauth/callback` routes, exchange code for access+refresh tokens, store per user, refresh on expiry; `YNABClient` already takes a bearer token so only token acquisition changes.

**Effort:** L
**Priority:** P2
**Depends on:** None

### Google Sign-In

**What:** Replace the password routes with an OIDC flow that sets the same `authed` session key for allowlisted emails.

**Why:** Single shared password doesn't scale past one user and can't be revoked per person.

**Context:** `app/auth.py` is the designed swap point; `require_login` stays as-is.

**Effort:** M
**Priority:** P3
**Depends on:** None

### Disconnect / unauthenticate from YNAB

**What:** A settings action that severs the YNAB connection.

**Why:** Users should be able to revoke the app's access from inside the app (rmillan advertises exactly this: "you can revoke this authorization at any moment").

**Context:** Today (PAT in `.env`) that's just docs — revoke the token in YNAB's Developer Settings and clear `YNAB_TOKEN`. Once YNAB OAuth lands it becomes a real feature: a "Disconnect from YNAB" button that deletes the stored access/refresh tokens and revokes the grant, with the UI returning to the "Connect to YNAB" state. Handle the revoked-token case gracefully everywhere either way — it's the same code path as a user revoking access from the YNAB side.

**Effort:** S
**Priority:** P3
**Depends on:** YNAB OAuth

### Multi-user

**What:** Per-user YNAB credentials and conversion lists.

**Why:** Opens the app to anyone, not just David.

**Context:** Order of work: YNAB OAuth first (per-user tokens), then real sign-in (Google via `auth.py`, or email+password like rmillan's Devise signup), then scope conversions by user. This is the point where `data/conversions.json` should become SQLite — don't add a database before this.

**Effort:** XL
**Priority:** P3
**Depends on:** YNAB OAuth, a real sign-in

## Infrastructure

### Deploy failure notifications

**What:** Have `deploy/autodeploy.sh` ping ntfy.sh/email on failed deploy or failed health check.

**Why:** Autodeploy failures currently only land in `~/autodeploy.log` on the server — silent until someone looks.

**Effort:** S
**Priority:** P2
**Depends on:** None

### Uptime monitoring

**What:** A free external monitor (UptimeRobot etc.) hitting `/healthz`.

**Why:** Currently nothing notices if the site is down.

**Effort:** S
**Priority:** P2
**Depends on:** None

### Deploy via GitHub Actions

**What:** A workflow job (after CI passes on master) that SSHes into the Linode and runs `deploy/autodeploy.sh` (or `git pull && docker compose up -d --build`).

**Why:** Deploys seconds after merge instead of within ~2 minutes, with logs visible in the Actions UI instead of `~/autodeploy.log`.

**Context:** Considered and skipped in v1 (see "Alternative considered" in DEPLOY.md) because it needs an SSH private key stored as a repo secret in a public repo, whereas the poller needs no credentials in either direction. If revisiting: use a dedicated deploy key restricted with an `authorized_keys` `command=` (forced command) so the key can only trigger the deploy script, nothing else; keep the cron poller as fallback or remove it to avoid double deploys. Sandbox caveat: agents can't SSH to the server, so setting up the key/secret is guided-manual with David.

**Effort:** M
**Priority:** P3
**Depends on:** None

### Back up `data/conversions.json` off-site

**What:** A one-line cron append to a private gist / rclone target.

**Why:** It's tiny and recreatable by hand, but a backup removes the "recreate from memory" step after a disk loss.

**Effort:** S
**Priority:** P3
**Depends on:** None

## Reference notes — ynab.rmillan.com investigation (2026-07-05)

Findings from actually signing up and poking at the reference site — useful
when working the OAuth, multi-user, and `(skipped)` items above:

- **Sign-up is instant** — email + password at
  https://ynab.rmillan.com/users/sign_up (Rails/Devise), no email
  confirmation. Creating a throwaway account to inspect behavior takes
  seconds; a mailinator address works. This is the way to verify the exact
  `(skipped)` memo format: connect a test YNAB budget, skip a transaction
  there, and look at what it writes to the memo.
- **"Connect to YNAB" is standard YNAB OAuth** (authorization-code flow):
  the button links to `https://app.youneedabudget.com/oauth/authorize` with
  their `client_id` and `redirect_uri=https://ynab.rmillan.com/oauth`. End
  users never need an API key — only the app owner registers an OAuth app
  (free, YNAB Developer Settings). Details folded into the YNAB OAuth item.
- **They store pending transactions server-side; we don't.** Their privacy
  copy: pending conversions are temporarily saved in their database until
  approved, cleaned up hourly if abandoned. Our preview→approve instead
  round-trips the proposed amounts/memos through hidden form fields, so
  nothing is persisted. Keep our stateless design — it's a feature, not a
  gap (relevant when building the scheduler, which must not quietly turn
  into server-side pending state).

## Completed

### Skip transactions + "already in budget currency" actions

Each preview row has an Action select — *Convert* (default), *Memo ≈…* for amounts entered in the budget currency already (keeps the amount, appends the original-currency equivalent with the FX-rate marker so it never reappears), and *Skip forever* (keeps the amount, appends `(skipped)`). Memos containing `(skipped)` are treated as not-to-convert (`is_skipped`, case-insensitive), so transactions skipped on rmillan's site should be respected too. Hardened per pre-landing review: byte-safe marker-preserving memo truncation, zero-rate guard, stale-form drop at apply, required action field, live footer totals, `| tojson` script escaping. The rmillan byte-format check lives on as its own open item above.

**Completed:** 2026-07-05 (branch `claude/mark-original-currency-qqyktt`)

### Edit a conversion

`/conversions/{id}/edit` (shared `conversion_form.html` with the new form), plus Edit/Delete on the detail page.

**Completed:** v1 (pre-2026-07-05)

### One conversion per account

The new/edit forms disable accounts that already have a conversion, and create/edit reject duplicates server-side with a 409.

**Completed:** v1 (pre-2026-07-05)

### Friendly error pages

Exception handlers in `main.py` render `error.html` (502) for `YNABError`/`RatesError`, including connection failures, with a hint and retry/back links.

**Completed:** v1 (pre-2026-07-05)

### Handle YNAB rate limiting (429)

429s render their own page explaining the ~200 req/hour budget. The remaining "delta requests" idea is now its own open item above.

**Completed:** v1 (pre-2026-07-05)

### Zero-decimal display bug in preview

The converted column formats via `format_amount(new_milliunits, to_currency)`.

**Completed:** v1 (pre-2026-07-05)

### Validate currency direction on create

Create and edit fetch the budget's `iso_code` from YNAB and reject mismatches with a 400.

**Completed:** v1 (pre-2026-07-05)

### Retries/backoff on outbound calls

`app/http.py` `get_with_retry` — one retry after 0.5s on connection errors and 502/503/504, GETs only (the YNAB PATCH is never retried).

**Completed:** v1 (pre-2026-07-05)

### CSRF tokens on POST forms

Per-session token via `csrf_input()` in every form + `verify_csrf` router dependency (403 on mismatch). Session cookie stays `SameSite=Lax` as a second layer.

**Completed:** v1 (pre-2026-07-05)

### Login rate limiting

In-memory counter in `auth.py`; after 5 consecutive failures each further failure doubles the lockout (cap 5 min), 429 with a countdown message meanwhile.

**Completed:** v1 (pre-2026-07-05)

### Security headers

Middleware sets X-Frame-Options, X-Content-Type-Options, Referrer-Policy, and a CSP (inline scripts allowed — the templates use small inline scripts, no external assets).

**Completed:** v1 (pre-2026-07-05)

### Run the container as non-root

uvicorn runs as uid 1000 (matches the first user on a stock Debian host so the bind-mounted `./data` stays writable; DEPLOY.md documents the `chown` fallback).

**Completed:** v1 (pre-2026-07-05)

### Public landing / home page

`/` renders `landing.html` (pitch, three steps, privacy note, log-in button) for anonymous visitors and redirects straight to `/conversions` when logged in. No screenshot yet — add one if the page ever needs selling power.

**Completed:** v1 (pre-2026-07-05)

### Mobile-friendly styling

First pass: below 700px the memo column is hidden, padding/font shrink, tables scroll horizontally, and the landing steps stack. Revisit a card layout only if that isn't comfortable enough in practice.

**Completed:** v1 (pre-2026-07-05)

### Totals row in preview

Table footer with both sums. Now recomputes live over ticked Convert rows ("Total to convert (N rows)") since the 2026-07-05 actions branch.

**Completed:** v1 (pre-2026-07-05); live recompute 2026-07-05

### Post-apply flash instead of a bare page

Apply redirects to the detail page with `?applied=N` and a flash; `applied.html` removed.

**Completed:** v1 (pre-2026-07-05)

### Dark mode

`prefers-color-scheme` variables in `style.css`.

**Completed:** v1 (pre-2026-07-05)

### Dependency updates

`.github/dependabot.yml` (pip + GitHub Actions, weekly); CI green = auto-deployable.

**Completed:** v1 (pre-2026-07-05)

### Expose the running version

`GET /healthz` (unauthenticated) returns `{status, version}` with the git SHA baked in at build time (Dockerfile `ARG GIT_SHA`, exported by `autodeploy.sh`); the page footer shows it too, and autodeploy verifies the live version matches the deployed SHA. The compose file gained a `healthcheck:` on it.

**Completed:** v1 (pre-2026-07-05)

### Add lint + type-check to CI

`ruff check` (E/F/W/I/UP/B, line length 100) + `mypy` before pytest. `ruff format --check` was considered and skipped — it fights the compact literal style (test fixtures especially) for no correctness benefit.

**Completed:** v1 (pre-2026-07-05)

### Test coverage for the fixed gaps

Split skipping, YNAB/Frankfurter error paths, retries, 429, zero-decimal display, CSRF, throttling (`tests/test_errors.py` and additions to the flow/convert tests).

**Completed:** v1 (pre-2026-07-05)

### Async or pooled HTTP clients

Done (the "pick one lifecycle" half): both clients are cached process-wide singletons with pooled connections. Going async remains a scheduler-time decision (folded into the scheduler item above).

**Completed:** v1 (pre-2026-07-05)
