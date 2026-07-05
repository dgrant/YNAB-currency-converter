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
- [ ] **Skip transactions + respect rmillan's `(skipped)` memo marker.**
      rmillan's site appears to append a `(skipped)` string to the memo of
      transactions the user chose not to convert (unverified — confirm the
      exact format first; see the rmillan notes at the bottom for how to
      test). Two parts: (a) treat memos containing that marker as
      not-to-convert in `build_preview` (like `MARKER_RE`), otherwise
      transactions skipped on rmillan's site reappear in every preview here
      forever; (b) add a "Skip" action in our preview that writes the same
      marker to the memo, so unticking a transaction can be made permanent.
      Keep the string byte-compatible with rmillan's, same as the FX marker.
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
- [ ] **Handle split transactions.** `build_preview` converts any transaction
      by its top-level `amount`, but a YNAB split's subtransactions must sum
      to the parent. Patching a split parent's amount will be rejected or
      leave the split inconsistent. At minimum detect
      `subtransactions`/category `Split` and skip with a note in the preview;
      properly, convert each subtransaction with the same rate and rounding
      such that they still sum to the converted parent.
- [ ] **Friendly error pages.** `YNABError` and `RatesError` currently
      surface as raw 500s. Catch them in the routes (or an exception handler)
      and render a message with a retry link — most likely failures are YNAB
      down, token revoked, or Frankfurter down.
- [ ] **Handle YNAB rate limiting (429).** The API allows ~200 requests/hour
      per token. Rare today, but the scheduler and pending-count badges will
      hit it. Respect 429s with a clear message; consider YNAB delta requests
      (`last_knowledge_of_server`) to cut request volume.
- [ ] **Zero-decimal display bug in preview.** `preview.html` renders the
      converted amount with a hardcoded `%.2f`. Correct for 2-decimal budget
      currencies, wrong if the *budget* currency is zero-decimal (e.g. a JPY
      budget shows `¥1817.00`). Format via `decimal_digits(to_currency)` like
      `format_original` does. The stored milliunits are already correct —
      display-only.
- [ ] **Validate currency direction on create.** Nothing checks that
      `to_currency` matches the budget's currency (it's just a form field the
      user could mismatch). Fetch the budget's `iso_code` server-side on
      create instead of trusting the form.
- [ ] **Retries/backoff on outbound calls.** Both httpx clients have a 30s
      timeout but no retry; a transient Frankfurter blip fails the whole
      preview. One retry with backoff on idempotent GETs is enough.

## Security

- [ ] **CSRF tokens on POST forms.** Session-cookie auth plus plain HTML
      forms means a malicious page could forge a POST (worst case: an
      unwanted apply — it can only write amounts/memos the attacker guesses,
      but still). Add a per-session token to all forms, or set the session
      cookie `SameSite=Strict` (check what `SessionMiddleware` sets today).
- [ ] **Login rate limiting.** `secrets.compare_digest` is already used, but
      there's no brute-force throttle on `/login`. A dumb in-memory counter
      with exponential delay is fine for single-user.
- [ ] **Security headers.** Add `X-Frame-Options`/CSP basics via middleware
      or the nginx vhost.
- [ ] **Run the container as non-root.** Dockerfile currently runs uvicorn as
      root; add a `USER` after installing deps (mind `data/` volume
      ownership).

## UX

- [ ] **Mobile-friendly styling.** Primary usage is from a phone. The preview
      table (7 columns) needs a responsive treatment — collapse to cards or
      hide the memo column on small screens.
- [ ] **Totals row in preview.** Sum of original and converted amounts, so a
      batch approval can be sanity-checked at a glance.
- [ ] **Post-apply flash instead of a bare page.** `applied.html` is a dead
      end; redirect back to the detail page with a "N updated" flash message,
      and offer "preview again".
- [ ] **Dark mode** (`prefers-color-scheme` in `style.css`).

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
      is guided-manual with David. Unauthenticated, no side effects, returns 200
      + version/SHA. Point `deploy/autodeploy.sh`'s health check and a
      `docker compose` `healthcheck:` at it instead of `/login`.
- [ ] **Deploy failure notifications.** Autodeploy failures only land in
      `~/autodeploy.log` on the server. Have the script ping ntfy.sh/email on
      failed deploy or failed health check.
- [ ] **Uptime monitoring.** A free external monitor (UptimeRobot etc.)
      hitting `/healthz` — currently nothing notices if the site is down.
- [ ] **Back up `data/conversions.json` off-site.** It's tiny, recreatable by
      hand, but a one-line cron append to a private gist / rclone target
      removes the "recreate from memory" step after a disk loss.
- [ ] **Dependency updates.** Enable Dependabot (pip + GitHub Actions) so the
      pinned requirements don't rot; CI green = auto-deployable.
- [ ] **Expose the running version.** Bake the git SHA into the image at
      build time and show it in the footer / `/healthz`, so "what's live" is
      checkable without SSH.

## Code health / CI

- [ ] **Add lint + type-check to CI.** `ruff check` + `ruff format --check`
      and mypy alongside pytest in `.github/workflows/ci.yml` (CI gates
      deploys, so this directly protects prod).
- [ ] **Test coverage for the gaps above** as they're fixed: split
      transactions, YNAB/Frankfurter error paths, 429 handling,
      zero-decimal budget display.
- [ ] **Async or pooled HTTP clients.** Routes are sync `def` (fine —
      FastAPI threadpools them), but `YNABClient` is constructed per request
      while `FrankfurterClient` is a cached global — pick one lifecycle.
      Revisit when the scheduler lands, since it will share these clients.

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
