# TODOs / future ideas

Roughly ordered by value within each section. v1 is live and works; nothing
here is required for day-to-day use. See CLAUDE.md for conventions that must
not break (memo marker format, milliunit math, preview→approve contract).

## Features

- [ ] **Daily auto-sync scheduler.** A background job (in-process
      `asyncio` task or a second container on cron) that runs preview for
      every conversion and either auto-applies or just records what's
      pending. Needs: YNAB error/429 handling first, and a decision on
      auto-apply vs. notify-only (start with notify-only — the preview→approve
      contract is a safety feature).
- [ ] **Notifications for pending conversions.** Pairs with the scheduler:
      when unconverted transactions appear, send an email / ntfy.sh / Telegram
      ping with the count and a link. Makes the app pull-free: enter
      transactions on the phone, get pinged, tap approve.
- [ ] **Undo / revert a conversion.** The memo marker contains everything
      needed to reverse an apply: original amount and rate. Parse
      `-1,817 JPY (FX rate: 0.0087987)` back out, restore the original
      milliunits, strip the marker from the memo. Per-transaction and
      whole-batch undo on the applied page.
- [x] **Skip transactions + "already in budget currency" actions.** Done
      (2026-07): each preview row has an Action select — *Convert* (default),
      *Memo ≈… (already \<CUR\>)* which keeps the amount and appends the
      original-currency equivalent with the FX-rate marker, and *Skip forever*
      which keeps the amount and appends `(skipped)`. Memos containing
      `(skipped)` are treated as not-to-convert (`is_skipped`,
      case-insensitive), so transactions skipped on rmillan's site are
      respected too. Hardened over three review passes: byte-safe
      marker-preserving memo truncation, zero/NaN-rate guard, stale/edited
      re-checks at apply time, `| tojson` script escaping. Remaining: confirm
      the exact rmillan `(skipped)` byte format via a throwaway account (see
      rmillan notes at the bottom) if strict compatibility matters. Also
      unverified against the live YNAB API (only mocks): that a memo-only
      PATCH leaves the amount unchanged, and whether the 500 memo cap counts
      bytes or characters — the truncation is byte-safe either way.
- [x] **Edit a conversion.** Done: `/conversions/{id}/edit` (shared
      `conversion_form.html` with the new form), plus Edit/Delete on the
      detail page.
- [ ] **Convert all accounts at once.** Today preview/apply is per
      conversion; add a "Preview all" on the index page that runs the
      preview for every configured conversion and shows one combined,
      grouped table with a single approve. Implementation notes: rate
      fetches stay per conversion (different currency pairs), but YNAB
      updates can share one bulk PATCH per budget
      (`update_transactions` already takes a list); keep per-row unticking,
      and keep the preview→approve hidden-field contract. This is also
      most of the groundwork for the scheduler item above.
- [ ] **Auto-advance `start_date` after apply.** Every preview refetches all
      transactions since the original start date and re-skips converted ones.
      After a successful apply, bump the stored `start_date` to the oldest
      still-unconverted date (or keep a `last_synced` field) to keep fetches
      small as history grows.
- [ ] **Show pending counts on the index page.** The conversions list is
      static config; a "N unconverted" badge per row (fetched on demand or by
      the scheduler) turns it into a dashboard.
- [ ] **Manual rate override in preview.** Editable rate per row (recompute
      amount client-side or on re-preview) for cash exchanged at a
      non-market rate, card FX fees, etc.
- [ ] **Google Sign-In.** `app/auth.py` is the designed swap point: replace
      the password routes with an OIDC flow that sets the same `user_id`
      session key (creating the user row on first sign-in); `require_login`
      stays as-is.
- [x] **YNAB OAuth.** Done: `/oauth/ynab/start` + `/oauth/ynab/callback`
      (authorization-code flow with state check), tokens stored per user,
      auto-refresh on expiry with a 60s margin (`app/oauth.py`). Activated
      by registering an OAuth app (free, YNAB Developer Settings; redirect
      URI `<origin>/oauth/ynab/callback`) and setting `YNAB_CLIENT_ID` /
      `YNAB_CLIENT_SECRET` (+ `PUBLIC_BASE_URL` behind the proxy) — not yet
      done for the live deployment, which needs David to register the app.
      Until then users paste personal access tokens.
### Lift the YNAB OAuth app out of Restricted Mode

A freshly registered OAuth app is token-capped (~25 access tokens), so
beyond a handful of connected users new "Connect to YNAB" authorizations
fail. Removing the cap means passing YNAB's OAuth App Review (Asana form).
Not blocking for friends-and-family scale — users can always paste a
personal access token on `/settings` (no cap). The review's prerequisites,
each as its own task:

- [ ] **Footer trademark disclaimer.** Add to every page footer
      (`templates/base.html`): "We are not affiliated, associated, or in
      any way officially connected with YNAB… The names YNAB and You Need A
      Budget… are registered trademarks of YNAB." (Exact wording is on the
      review form.)
- [ ] **Real Privacy Policy page.** Add a `/privacy` route + page that
      explains how data obtained through the YNAB API is handled, stored,
      and secured (today it's only the landing-page blurb). Link it from the
      footer and use its URL in Developer Settings + the review form.
- [ ] **"Plan" not "budget" branding.** YNAB brand language prefers "plan"
      over "budget" where applicable; the UI/copy say "budget" throughout.
      Decide how far to reword, and make sure nothing implies YNAB
      endorsement.
- [ ] **Confirm name uniqueness + logo rules.** App name must not already
      be on the Works With YNAB list; no YNAB logos except the authorized
      "Works with YNAB" logo (we use none today — just verify).
- [ ] **Submit the OAuth App Review form** (Asana) once the above are live,
      to have Restricted Mode removed. Auth is already OAuth-only, which the
      form requires.
- [x] **Multi-user.** Done (2026-07): email+password signup like rmillan's,
      per-user YNAB credentials (OAuth or PAT), conversions scoped by
      `user_id`, all in SQLite (`data/app.db` — users, ynab_connections,
      conversions). `python -m app.import_legacy <email>` migrates a v1
      deployment. Follow-ups worth considering: password reset (needs
      outbound email), account deletion, validating a pasted PAT against
      `GET /user` at save time, and signup abuse controls if it's ever
      opened up beyond friends & family.
- [ ] **Crypto / non-ECB currencies.** Frankfurter is ~30 fiat currencies
      (ECB data). Add a second rate source behind the `RateTable` interface
      (e.g. CoinGecko for crypto) and pick per conversion.

## Correctness & robustness

- [ ] **Don't block the event loop with sync YNAB calls in async handlers.**
      `apply()` is `async def` but calls the synchronous `YNABClient`
      (`httpx.Client`) for `get_transactions` + `update_transactions` without
      awaiting, so one user's apply blocks the single event loop — and thus
      every other user's request — for up to two sequential ~30s YNAB
      round-trips. Pre-existing, but the multi-user merge turned a self-inflicted
      stall into cross-tenant availability coupling. Fix: wrap the blocking calls
      in `await run_in_threadpool(...)` (or make `YNABClient` async). Note this
      also makes the `_apply_lock` in `apply()` actually necessary — today the
      sync I/O already prevents interleaving, so the lock is a no-op guard that
      only starts doing real work once the I/O yields.
- [x] **One conversion per account.** Done: the new/edit forms disable
      accounts that already have a conversion, and create/edit reject
      duplicates server-side with a 409.
- [ ] **Convert split transactions properly.** The dangerous half is fixed:
      splits (non-empty `subtransactions`) are now detected, skipped in
      `build_preview`, and counted with a note in the preview, so apply can
      no longer corrupt them. Remaining feature work: convert each
      subtransaction with the same rate, rounding such that they still sum
      to the converted parent, then PATCH parent + subtransactions together.
- [x] **Friendly error pages.** Done: exception handlers in `main.py` render
      `error.html` (502) for `YNABError`/`RatesError`, including connection
      failures, with a hint and retry/back links.
- [x] **Handle YNAB rate limiting (429).** Done: 429s render their own page
      explaining the ~200 req/hour budget. Still worth adding YNAB delta
      requests (`last_knowledge_of_server`) to cut request volume when the
      scheduler / pending-count badges land.
- [x] **Zero-decimal display bug in preview.** Done: the converted column
      formats via `format_amount(new_milliunits, to_currency)`.
- [x] **Validate currency direction on create.** Done: create and edit fetch
      the budget's `iso_code` from YNAB and reject mismatches with a 400.
- [x] **Retries/backoff on outbound calls.** Done: `app/http.py`
      `get_with_retry` — one retry after 0.5s on connection errors and
      502/503/504, GETs only (the YNAB PATCH is never retried).

## Security

- [x] **CSRF tokens on POST forms.** Done: per-session token via
      `csrf_input()` in every form + `verify_csrf` router dependency (403 on
      mismatch). Session cookie stays `SameSite=Lax` as a second layer.
- [x] **Login rate limiting.** Done: in-memory counter in `auth.py`; after 5
      consecutive failures each further failure doubles the lockout (cap
      5 min), 429 with a countdown message meanwhile.
- [x] **Security headers.** Done: middleware sets X-Frame-Options,
      X-Content-Type-Options, Referrer-Policy, and a CSP (inline scripts
      allowed — the templates use small inline scripts, no external assets).
- [x] **Run the container as non-root.** Done: uvicorn runs as uid 1000
      (matches the first user on a stock Debian host so the bind-mounted
      `./data` stays writable; DEPLOY.md documents the `chown` fallback).

## UX

- [x] **Public landing / home page.** Done: `/` now renders `landing.html`
      (pitch, three steps, privacy note, log-in button) for anonymous
      visitors and redirects straight to `/conversions` when logged in.
      No screenshot yet — add one if the page ever needs selling power.
- [x] **Mobile-friendly styling.** First pass done: below 700px the memo
      column is hidden, padding/font shrink, tables scroll horizontally, and
      the landing steps stack. Revisit a card layout only if that isn't
      comfortable enough in practice.
- [x] **Totals row in preview.** Done: table footer with both sums (covers
      all listed rows; unticking doesn't recompute — it's a sanity check).
- [x] **Post-apply flash instead of a bare page.** Done: apply redirects to
      the detail page with `?applied=N` and a flash; `applied.html` removed.
- [x] **Dark mode.** Done (`prefers-color-scheme` variables in `style.css`).

## Ops / deployment

- [ ] **Rotate the YNAB OAuth client secret.** The secret was visible in a
      screenshot during setup (2026-07), so treat it as exposed. Regenerate
      it in YNAB → Developer Settings → the OAuth app, then update
      `YNAB_CLIENT_SECRET` in the server `.env` (re-run `set-ynab-oauth.sh`
      or edit directly) and `docker compose up -d --force-recreate`. Already-
      minted access/refresh tokens keep working, so connected users stay
      connected through the rotation.
- [ ] **Remove the dead single-user secrets from the server `.env`.**
      `APP_PASSWORD` and `YNAB_TOKEN` are no longer read by the app after the
      multi-user migration (only `app.import_legacy` ever used them, and that
      has run). Delete both from `.env`, and revoke the old `YNAB_TOKEN`
      personal access token in YNAB → Developer Settings so the long-lived
      credential can't be used.
- [ ] **Deploy via GitHub Actions** instead of the server-side cron poller.
      A workflow job (after CI passes on master) that SSHes into the Linode
      and runs `deploy/autodeploy.sh` (or `git pull && docker compose up -d
      --build`) — deploys seconds after merge instead of within ~2 minutes,
      with logs visible in the Actions UI instead of `~/autodeploy.log`.
      This was considered and skipped in v1 (see "Alternative considered" in
      DEPLOY.md) because it needs an SSH private key stored as a repo secret
      in a public repo, whereas the poller needs no credentials in either
      direction. If revisiting: use a dedicated deploy key restricted with
      an `authorized_keys` `command=` (forced command) so the key can only
      trigger the deploy script, nothing else; keep the cron poller as
      fallback or remove it to avoid double deploys. Note the sandbox
      caveat: agents can't SSH to the server, so setting up the key/secret
      is guided-manual with David.
- [ ] **Deploy failure notifications.** Autodeploy failures only land in
      `~/autodeploy.log` on the server. Have the script ping ntfy.sh/email on
      failed deploy or failed health check.
- [ ] **Uptime monitoring.** A free external monitor (UptimeRobot etc.)
      hitting `/healthz` — currently nothing notices if the site is down.
- [ ] **Back up `data/app.db` off-site.** It now holds user accounts and
      YNAB credentials, not just conversion configs, so it's no longer
      recreatable from memory — a nightly cron copy (`sqlite3 app.db
      ".backup ..."`) to an rclone target is worth doing. (Was: back up
      `conversions.json`.)
- [x] **Dependency updates.** Done: `.github/dependabot.yml` (pip + GitHub
      Actions, weekly); CI green = auto-deployable.
- [x] **Expose the running version.** Done: `GET /healthz` (unauthenticated)
      returns `{status, version}` with the git SHA baked in at build time
      (Dockerfile `ARG GIT_SHA`, exported by `autodeploy.sh`); the page
      footer shows it too, and autodeploy verifies the live version matches
      the deployed SHA. The compose file gained a `healthcheck:` on it.

## Code health / CI

- [x] **Add lint + type-check to CI.** Done: `ruff check` (E/F/W/I/UP/B,
      line length 100) + `mypy` before pytest. `ruff format --check` was
      considered and skipped — it fights the compact literal style (test
      fixtures especially) for no correctness benefit.
- [x] **Test coverage for the gaps above.** Done for everything fixed so
      far: split skipping, YNAB/Frankfurter error paths, retries, 429,
      zero-decimal display, CSRF, throttling (`tests/test_errors.py` and
      additions to the flow/convert tests).
- [x] **Async or pooled HTTP clients.** Done (the "pick one lifecycle"
      half): both clients are now cached process-wide singletons with pooled
      connections. Going async remains a scheduler-time decision.

## Notes from investigating ynab.rmillan.com (2026-07-05)

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
