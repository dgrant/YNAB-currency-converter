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
      whole-batch undo on the applied page. **Must exclude `≈ …` equivalence
      markers** (the "already in budget currency" action): those amounts were
      never changed, so "restoring" the parsed value would corrupt them.
- [x] **Skip transactions + "already in budget currency" actions.** Done:
      each preview row has an Action select — *Convert* (default),
      *Already \<CUR\> (memo ≈… )* for amounts entered in the budget currency
      already (keeps the amount, appends the original-currency equivalent
      with the FX-rate marker so it never reappears), and *mark skipped*
      (keeps the amount, appends `(skipped)`). Memos containing `(skipped)`
      are treated as not-to-convert (`is_skipped`), so transactions skipped
      on rmillan's site should be respected too. Remaining: the exact rmillan
      `(skipped)` byte format is still unverified — confirm via a throwaway
      rmillan account (see notes at the bottom) if compatibility matters.
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
      the password routes with an OIDC flow that sets the same `authed`
      session key for allowlisted emails; `require_login` stays as-is.
- [ ] **YNAB OAuth** instead of a personal access token. Prerequisite for
      multi-user; also removes the long-lived token from `.env`. Confirmed
      how ynab.rmillan.com does it (2026-07): a "Connect to YNAB" button
      linking to `https://app.youneedabudget.com/oauth/authorize?client_id=…
      &response_type=code&redirect_uri=https://ynab.rmillan.com/oauth` —
      the standard authorization-code flow. End users need no API key; they
      just click Authorize in YNAB. The app owner registers an OAuth
      application once (free, YNAB Developer Settings) to get the
      client id/secret and set the redirect URI. Implementation here:
      `/oauth/start` + `/oauth/callback` routes, exchange code for
      access+refresh tokens, store per user, refresh on expiry;
      `YNABClient` already takes a bearer token so only token acquisition
      changes.
- [ ] **Disconnect / unauthenticate from YNAB.** A settings action that
      severs the YNAB connection. Today (PAT in `.env`) that's just docs —
      revoke the token in YNAB's Developer Settings and clear `YNAB_TOKEN`.
      Once YNAB OAuth lands (item above) it becomes a real feature: a
      "Disconnect from YNAB" button that deletes the stored access/refresh
      tokens and revokes the grant, with the UI returning to the
      "Connect to YNAB" state (rmillan advertises exactly this:
      "you can revoke this authorization at any moment"). Handle the
      revoked-token case gracefully everywhere either way — it's the same
      code path as a user revoking access from the YNAB side.
- [ ] **Multi-user.** Per-user YNAB credentials and conversion lists. Order
      of work: YNAB OAuth first (per-user tokens), then real sign-in
      (Google via `auth.py`, or email+password like rmillan's Devise
      signup), then scope conversions by user. This is the point where
      `data/conversions.json` should become SQLite — don't add a database
      before this.
- [ ] **Crypto / non-ECB currencies.** Frankfurter is ~30 fiat currencies
      (ECB data). Add a second rate source behind the `RateTable` interface
      (e.g. CoinGecko for crypto) and pick per conversion.

## Correctness & robustness

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
- [ ] **Back up `data/conversions.json` off-site.** It's tiny, recreatable by
      hand, but a one-line cron append to a private gist / rclone target
      removes the "recreate from memory" step after a disk loss.
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
