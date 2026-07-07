# TODOS

Reorganized (2026-07) into gstack's canonical template (H3 items with
What/Why/Context/Effort/Priority, completed items moved to `## Completed`).
Versioning uses gstack's four-part `MAJOR.MINOR.PATCH.MICRO` scheme in the
root `VERSION` file, with human-readable release notes in `CHANGELOG.md`;
`/healthz` and the page footer report it directly (see CLAUDE.md "Deploy").
The git SHA baked into the image at build time still exists, but only as
internal plumbing (cache-busting, exact-commit deploy verification) — it's
no longer user-facing. Completed items below predate the `VERSION` file and
stay annotated with a date; new ones can cite a semver.
See CLAUDE.md for conventions that must not break (memo marker format,
milliunit math, preview→approve contract).

## Features

### Submit the OAuth App Review form

**What:** File the YNAB OAuth App Review (Asana form) now that every
prerequisite has landed.

**Why:** A freshly registered OAuth app is token-capped (~25 access
tokens); beyond a handful of connected users, new "Connect to YNAB"
authorizations fail. Clearing Restricted Mode is the only way to grow past
friends-and-family scale.

**Context:** All review prerequisites are done: footer trademark
disclaimer, real Privacy Policy page, "Plan" not "budget" branding, name
uniqueness confirmed against the Works With YNAB list, and auth is
OAuth-only (the form requires this). Nothing else blocks submission.

**Effort:** S
**Priority:** P1
**Depends on:** None (all prerequisites already shipped — see Completed)

### Convert split transactions properly

**What:** Convert each subtransaction of a split with the same rate,
rounding so they still sum to the converted parent, then PATCH parent +
subtransactions together.

**Why:** Split transactions are currently skipped entirely — a real
correctness gap for any account where splits are common (e.g. a shared
card).

**Context:** The dangerous half is already fixed: splits (non-empty
`subtransactions`) are detected, skipped in `build_preview`, and counted
with a note in the preview, so apply can no longer corrupt them. This item
is the remaining feature work to actually convert them instead of skipping.

**Effort:** L
**Priority:** P1

### Convert all accounts at once

**What:** Add a "Preview all" on the index page that runs preview for
every configured conversion and shows one combined, grouped table with a
single approve.

**Why:** Today preview/apply is per-conversion; this removes the need to
click through each account separately.

**Context:** Rate fetches stay per conversion (different currency pairs),
but YNAB updates can share one bulk PATCH per budget
(`update_transactions` already takes a list). Keep per-row unticking, and
keep the preview→approve hidden-field contract. This is also most of the
groundwork for the auto-sync scheduler below.

**Effort:** M
**Priority:** P1

### Daily auto-sync scheduler

**What:** A background job (in-process `asyncio` task or a second
container on cron) that runs preview for every conversion and either
auto-applies or just records what's pending.

**Why:** Makes the app closer to zero-touch — enter transactions on the
phone, and the sync happens without a manual visit.

**Context:** Needs YNAB error/429 handling first (already done — see
Completed), and a decision on auto-apply vs. notify-only. Start with
notify-only: the preview→approve contract is a safety feature and
shouldn't be silently bypassed.

**Effort:** L
**Priority:** P2
**Depends on:** Convert all accounts at once (shares the groundwork)

### Show pending counts on the index page

**What:** A "N unconverted" badge per row (fetched on demand or by the
scheduler), turning the static conversions list into a dashboard.

**Why:** So you can see at a glance which accounts have new transactions to
convert and go deal with them. Today the index is just static config — it
gives no signal about which accounts actually need attention, so you have
to open each conversion and run a preview to find out.

**Context:** Pairs naturally with the scheduler and `last_synced` work
already shipped. Request volume is the thing to watch: a naive
render-time count fetches transactions for every conversion on each page
load (see "Cap or chunk large previews / applies" and the ~200/hr YNAB
budget), so fetch on demand or off the scheduler rather than inline.

**Effort:** M
**Priority:** P1

### Notifications for pending conversions

**What:** When unconverted transactions appear, send an email / ntfy.sh /
Telegram ping with the count and a link.

**Why:** Pairs with the scheduler to make the app pull-free: enter
transactions, get pinged, tap approve.

**Context:** Needs outbound email/notification infrastructure, which
password reset (below) also needs — worth building once and sharing.

**Effort:** M
**Priority:** P2
**Depends on:** Daily auto-sync scheduler

### Undo / revert a conversion

**What:** Parse the memo marker (`-1,817 JPY (FX rate: 0.0087987)`) back
out, restore the original milliunits, and strip the marker from the memo.
Per-transaction and whole-batch undo on the applied page.

**Why:** Mistakes happen (wrong rate, wrong account) and there's currently
no way back except manual YNAB edits.

**Context:** The memo marker already contains everything needed to
reverse an apply — this is purely a UI + PATCH feature, no new data model.

**Caveat (worth deciding before building):** the memo records the *display*
amount (`format_original`, rounded to the currency's minor unit), not the
original milliunits. For whole/minor-unit amounts the round-trip is exact,
but a sub-minor-unit original (e.g. a fractional-cent import, `-45.305 EUR`
stored as `-45305`) would come back rounded (`-45.31` → `-45310`),
silently corrupting the amount on undo. Options: (a) accept
display-precision undo and document it, (b) store the original milliunits in
the DB at apply time for a lossless undo, or (c) only offer undo when the
parsed amount round-trips to the current YNAB amount. Deferred pending that
call rather than shipping a silent-corruption path.

**Effort:** M
**Priority:** P2

### Password reset

**What:** Self-service password reset flow.

**Why:** Currently there's no recovery path for a forgotten password
beyond asking David directly.

**Context:** Needs outbound email, which the notifications work also
needs — noted as a follow-up under the original multi-user item, broken
out here as its own task.

**Effort:** M
**Priority:** P2
**Depends on:** Shares email infrastructure with Notifications for pending conversions

### Manual rate override in preview

**What:** An editable rate per row in preview (recompute amount
client-side or on re-preview).

**Why:** Cash exchanged at a non-market rate, or card FX fees, mean the
Frankfurter rate isn't always the right one.

**Context:** No blockers; touches `build_preview` and the preview
template/form contract.

**Effort:** M
**Priority:** P3

### Google Sign-In

**What:** Replace the password routes with an OIDC flow that sets the
same `user_id` session key (creating the user row on first sign-in).

**Why:** Removes password-management friction for users who'd rather use
an existing Google account.

**Context:** `app/auth.py` is the designed swap point; `require_login`
stays as-is. No urgency — email+password works fine today.

**Effort:** L
**Priority:** P3

### Crypto / non-ECB currencies

**What:** Add a second rate source behind the `RateTable` interface
(e.g. CoinGecko for crypto) and pick per conversion.

**Why:** Frankfurter only covers ~30 fiat currencies (ECB data) — no
crypto, no minor fiat currencies outside the ECB basket.

**Context:** `RateTable` is already an interface boundary designed for
this kind of swap-in.

**Effort:** L
**Priority:** P4

## Correctness & robustness

### Reject same-currency conversions

**What:** Reject creating (or editing) a conversion whose `from_currency`
equals the plan's derived `to_currency`, and skip such rows in
batch-create. Ideally surface it in the form UI too (disable/flag the
matching currency once the plan currency is known).

**Why:** A conversion from a currency to itself is a no-op that can only
do harm: Frankfurter has no self-pair rate to fetch (so preview errors),
and even if it returned 1.0 it would rewrite amounts and stamp memos for
nothing. It's never a valid config, so it should be rejected up front
rather than failing later at preview.

**Context:** `to_currency` is already derived from the plan
(`_budget_currency`) rather than posted, and create/edit already reject a
wrong *direction* mismatch — this is the adjacent equal-currency case that
check doesn't cover. The natural home is alongside that validation in the
create/edit handlers in `routes/conversions.py` (a 400, like the existing
direction check), plus the skip path in batch-create where other invalid
rows are already dropped rather than failing the whole batch.

**Effort:** S
**Priority:** P2

### Cap or chunk large previews / applies

**What:** Bound the work a single preview/apply does when a conversion's
`start_date` pulls in a very large number of transactions — e.g. paginate
the preview table and split the apply into fixed-size PATCH batches instead
of one unbounded bulk PATCH.

**Why:** Everything on the preview→apply path scales linearly with
transaction count and is currently uncapped (unlike bulk-delete, which caps
at 200 ids). A really old `start_date` on a busy account produces one huge
preview page whose proposed amounts/memos are all round-tripped through
hidden form fields (large page + large approve POST), and apply then sends
every update as a single all-or-nothing PATCH that is never retried — so a
timeout or rejection on a big batch applies nothing. Request *volume* is
fine (fetch + rates + PATCH is ~constant regardless of count), so this is
about payload size and failure blast radius, not rate limits.

**Context:** `preview()` builds one row per pending txn into `preview.html`;
`apply()` sends `safe` to `ynab.update_transactions` in one PATCH
(`ynab.py:75`, deliberately not retried). Chunking apply must preserve the
preview→approve hidden-field contract and the per-conversion apply lock,
and mark `last_synced` / advance `start_date` only after all chunks
succeed (or define partial-success semantics). Low urgency: the post-apply
`start_date` auto-advance means only the *first* oversized run hurts, and
friends-and-family accounts rarely hit it. Pairs with "Default the start
date earlier than today," which makes big first previews more common.

**Effort:** M
**Priority:** P4

### Shared test for the currency-guess heuristic (Python + JS)

**What:** The account-name-to-currency-code guess (e.g. "Chequing USD" →
preselect USD) is implemented twice — `_guess_currency` in
`routes/conversions.py` for the batch form, and inline JS in
`conversion_form.html` for the single new/edit form — with no shared test
asserting the two agree on the same account names.

**Why:** A future edit to one implementation (e.g. the regex/split logic)
could silently diverge from the other, so the same account name gets
guessed differently depending on which form the user is on.

**Context:** Found during review of the batch-create feature (2026-07).
Not urgent — both implementations are simple and were added together — but
worth a shared fixture list of account names run through both, or unifying
on one implementation, before either one changes again.

**Effort:** S
**Priority:** P3

*(All other items in this section are done — see Completed. The one other
open correctness item, "Convert split transactions properly," is filed
under Features above since it's feature work on top of an already-fixed
safety issue.)*

## Security

*(All items in this section are done — see Completed.)*

## UX

### Default the start date earlier than today

**What:** Prefill the new-conversion and batch-create forms with a start
date some way in the past (e.g. ~30 days back, or the start of the current
month) instead of today.

**Why:** `start_date` is the fetch floor — transactions dated before it are
never pulled. Defaulting to today means a fresh conversion silently ignores
every transaction already entered before setup, which is exactly the
backlog a new user wants converted first. They have to notice the default
and manually pick an earlier date to catch anything.

**Context:** Both defaults currently come from the same `today` value
(`_form_context` → `date.today().isoformat()`), used in
`conversion_form.html` and `batch_form.html`. This only changes the
*prefilled* value; the field stays editable and `_validate_start_date`
is unaffected. Pick the lookback window (fixed N days vs. start-of-month)
when building it.

**Effort:** S
**Priority:** P3

## Ops / deployment

### Rotate the YNAB OAuth client secret

**What:** Regenerate the OAuth client secret in YNAB → Developer Settings,
update `YNAB_CLIENT_SECRET` in the server `.env`, and
`docker compose up -d --force-recreate`.

**Why:** The secret was visible in a screenshot during setup (2026-07), so
it must be treated as exposed.

**Context:** Already-minted access/refresh tokens keep working through the
rotation, so connected users stay connected — this is a pure secret-rotation
op, not user-facing. Agents can't SSH to the server; this is guided-manual
with David via the Linode Lish console.

**Effort:** S
**Priority:** P0

### Back up `data/app.db` off-site

**What:** A nightly cron copy (`sqlite3 app.db ".backup ..."`) to an
rclone target.

**Why:** The database now holds user accounts and YNAB credentials, not
just conversion configs — it's no longer recreatable from memory if lost.

**Context:** Was previously "back up `conversions.json`" before the
multi-user migration; needs updating for the new SQLite file.

**Effort:** S
**Priority:** P1

### Remove the dead single-user secrets from the server `.env`

**What:** Delete `APP_PASSWORD` and `YNAB_TOKEN` from `.env`, and revoke
the old `YNAB_TOKEN` personal access token in YNAB → Developer Settings.

**Why:** Neither is read by the app anymore after the multi-user
migration (only `app.import_legacy` ever used them, and that has run) — the
long-lived PAT credential should be revoked so it can't be used if leaked.

**Context:** Pure cleanup; no functional risk to leaving them, but no
reason to keep a live, unused credential around either.

**Effort:** S
**Priority:** P1

### Uptime monitoring

**What:** A free external monitor (UptimeRobot etc.) hitting `/healthz`.

**Why:** Currently nothing notices if the site is down.

**Context:** `/healthz` already exists and returns `{status, version}` —
this is just wiring up an external monitor, no app changes needed.

**Effort:** S
**Priority:** P2

### Deploy failure notifications

**What:** Have `autodeploy.sh` ping ntfy.sh/email on a failed deploy or
failed health check.

**Why:** Autodeploy failures only land in `~/autodeploy.log` on the
server today — nobody is proactively notified.

**Context:** Shares notification infrastructure with the in-app
notifications feature above, if that lands first.

**Effort:** S
**Priority:** P2

### Deploy via GitHub Actions

**What:** A workflow job (after CI passes on master) that SSHes into the
Linode and runs `deploy/autodeploy.sh` (or `git pull && docker compose up
-d --build`).

**Why:** Deploys seconds after merge instead of within ~2 minutes, with
logs visible in the Actions UI instead of `~/autodeploy.log`.

**Context:** Considered and skipped in v1 (see "Alternative considered"
in DEPLOY.md) because it needs an SSH private key stored as a repo secret
in a public repo, whereas the poller needs no credentials in either
direction. If revisiting: use a dedicated deploy key restricted with an
`authorized_keys` `command=` (forced command) so the key can only trigger
the deploy script, nothing else; keep the cron poller as fallback or
remove it to avoid double deploys. Agents can't SSH to the server, so
setting up the key/secret is guided-manual with David.

**Effort:** M
**Priority:** P3

### Audit log

**What:** Record security/data-relevant events (login, conversion
created/edited/deleted, apply, YNAB connect/disconnect) with user id +
timestamp.

**Why:** For debugging and accountability as the user base grows past
friends-and-family.

**Context:** No existing infra for this; would need a new table or
append-only log file.

**Effort:** M
**Priority:** P3

### Per-user metrics

**What:** Track e.g. transactions processed/converted per user,
conversions configured, last activity.

**Why:** Both for David's own insight and to power the admin view below.

**Context:** No blockers; straightforward aggregation queries over
existing tables plus whatever the audit log adds.

**Effort:** M
**Priority:** P3

### Admin interface

**What:** A minimal admin-only view of users and their activity/metrics.

**Why:** Enough to see who's using the site and to help a user who emails
in because they're stuck.

**Context:** Needs an admin flag on the user row. Most useful once
per-user metrics exist to show.

**Effort:** M
**Priority:** P3
**Depends on:** Per-user metrics (for something to display)

### Switch to a real database

**What:** Consider Postgres behind a thin data layer if usage grows.

**Why:** SQLite (stdlib `sqlite3`, no ORM) is fine for friends-and-family
scale but a single-file, single-writer store caps concurrency (see the
single-worker constraint) and complicates backups/migrations.

**Context:** `db.py` is the deliberate swap point for this. No urgency —
only worth doing if usage actually grows past friends-and-family scale.

**Effort:** XL
**Priority:** P4

## Code health / CI

*(All items in this section are done — see Completed.)*

---

## Completed

### Batch-create conversions for multiple accounts at once
**(Features)**

Done (2026-07): `/conversions/batch` (GET lists every not-yet-configured
account across all plans, one row each, with the original currency
guessed from the account name — `_guess_currency`, the server-side twin of
the new-form JS — and a start-date default; POST creates all ticked rows in
one go). Plan/account names and the target currency are resolved from YNAB
at submit time (`_account_index`), not trusted from the form, same as the
derived-plan-currency work. Already-configured, unknown, or duplicate rows
are skipped rather than failing the batch; the index shows an "N created"
flash and gained a "Batch add" button. Tests: `test_batch_create_conversions`
(guess preselect, derivation, skip-configured, re-submit skip) and
`test_batch_create_requires_csrf`.

**Completed:** v0.2.0.0 (2026-07-07)

### Auto-advance `start_date` after apply
**(Features)**

Done (2026-07): after a successful apply, `apply()` advances the stored
`start_date` (via the new `store.set_start_date`) up to the oldest
transaction still needing attention — the min date among fetched
transactions that are neither excluded (converted/skipped/zero) nor part of
this apply. That deliberately includes splits we can't convert yet and rows
left unticked or dropped by the stale/edited re-checks, so the floor never
advances past anything still pending. When nothing is left pending it moves
to today (the `last_synced` floor). It only ever moves forward. This is the
same `start_date`-as-fetch-floor model the app already relies on
(transactions dated before it are never fetched); a transaction *backdated*
earlier than the new floor and entered later won't be picked up
automatically — widen `start_date` via Edit if you backdate. The robust
fix for that is YNAB delta requests (`last_knowledge_of_server`), noted
under the scheduler/pending-count items. Tests:
`test_apply_advances_start_date_*` in `test_app_flow.py`.

**Completed:** v0.2.0.0 (2026-07-07)

### Derive the plan currency instead of letting the user set it
**(Features)**

Done (2026-07): create/edit now read the target currency straight from the
plan in YNAB (`_budget_currency`) instead of a form field, so the
user-picked-vs-actual mismatch can no longer happen. The `to_currency`
`<select>` is gone from `conversion_form.html`, replaced by a read-only
display the form's JS fills from `budget.currency`; an unknown budget (or a
plan with no currency set) is still a 400. Replaces the old
`_validate_to_currency` mismatch check. Test:
`test_to_currency_is_derived_from_the_plan`.

**Completed:** v0.2.0.0 (2026-07-07)

### Skip transactions + "already in budget currency" actions
**(Features)**

Done (2026-07): each preview row has an Action select — *Convert*
(default), *Memo ≈… (already \<CUR\>)* which keeps the amount and appends
the original-currency equivalent with the FX-rate marker, and *Skip
forever* which keeps the amount and appends `(skipped)`. Memos containing
`(skipped)` are treated as not-to-convert (`is_skipped`, case-insensitive),
so transactions skipped on rmillan's site are respected too. Hardened over
three review passes: byte-safe marker-preserving memo truncation,
zero/NaN-rate guard, stale/edited re-checks at apply time, `| tojson`
script escaping. Remaining: confirm the exact rmillan `(skipped)` byte
format via a throwaway account (see rmillan notes at the bottom) if strict
compatibility matters. Also unverified against the live YNAB API (only
mocks): that a memo-only PATCH leaves the amount unchanged, and whether the
500 memo cap counts bytes or characters — the truncation is byte-safe
either way.

**Completed:** 2026-07

### Edit a conversion
**(Features)**

Done: `/conversions/{id}/edit` (shared `conversion_form.html` with the new
form), plus Edit/Delete on the detail page.

**Completed:** 2026-07

### YNAB OAuth
**(Features)**

Done: `/oauth/ynab/start` + `/oauth/ynab/callback` (authorization-code
flow with state check), tokens stored per user, auto-refresh on expiry
with a 60s margin (`app/oauth.py`). Activated by registering an OAuth app
(free, YNAB Developer Settings; redirect URI
`<origin>/oauth/ynab/callback`) and setting `YNAB_CLIENT_ID` /
`YNAB_CLIENT_SECRET` (+ `PUBLIC_BASE_URL` behind the proxy). Registered and
live for the deployment (2026-07). OAuth is now the *only* connection type
— the personal-access-token path was removed (2026-07) so the app is
truthfully OAuth-only for the App Review.

**Completed:** 2026-07

### Footer trademark disclaimer
**(Features — OAuth App Review prerequisite)**

Done (2026-07): every page footer (`templates/base.html`) carries the
standard "not affiliated… registered trademarks of YNAB" disclaimer.
Verify the wording matches the current review form before submitting — the
standard language was used, not a copy pasted from the form.

**Completed:** 2026-07

### Real Privacy Policy page
**(Features — OAuth App Review prerequisite)**

Done (2026-07): `/privacy` route + `privacy.html` (public, linked from the
footer) explaining what YNAB-API data is handled, what is not stored
(stateless preview→approve), and how it's secured. Use its URL in
Developer Settings + the review form.

**Completed:** 2026-07

### "Plan" not "budget" branding
**(Features — OAuth App Review prerequisite)**

Done (2026-07): user-facing copy referring to the YNAB plan entity now
says "Plan"/"plan" (index and detail labels, new/edit form labels, landing
+ index copy). Code identifiers and YNAB API field names (`budget_id`,
`budget_name`, the JS `budgets`) are left as-is since the API itself calls
them budgets. The landing headline keeps the verb "Budget in yours."
(generic English, not the YNAB noun). Nothing implies YNAB endorsement.

**Completed:** 2026-07

### Confirm name uniqueness + logo rules
**(Features — OAuth App Review prerequisite)**

Done (2026-07): checked the full Works With YNAB list — nothing named
"Currency Converter" or with convert/exchange/FX in it; the only currency
app is "Multi-currency for YNAB" (ynab.rmillan.com). Renamed the app "YNAB
Currency Converter" → "Currency Converter for YNAB" so it follows the
"‹Name› for YNAB" pattern instead of leading with the trademark (which can
read as official/endorsed), and dropped the landing tagline that
duplicated rmillan's exact app name. We use no YNAB logos. If David prefers
a different compliant name, change the template titles + nav brand +
`main.py` FastAPI title.

**Completed:** 2026-07

### Multi-user
**(Features)**

Done (2026-07): email+password signup like rmillan's, per-user YNAB
credentials (OAuth — the PAT path was removed 2026-07), conversions scoped
by `user_id`, all in SQLite (`data/app.db` — users, ynab_connections,
conversions). `python -m app.import_legacy <email>` migrates a v1
deployment. Follow-ups worth considering: account deletion (password reset
is now its own task above) and signup abuse controls if it's ever opened up
beyond friends & family.

**Completed:** 2026-07

### Last-synced date per account
**(Features)**

Done (2026-07): a `last_synced` column on `conversions` (nullable;
`db._apply_migrations` ALTER-adds it to existing DBs), set to today's date
by both preview and apply (`store.mark_synced`) — only after the operation
each one certifies actually succeeds (a rates failure or a rejected PATCH
must not falsely claim "synced"; caught and fixed via adversarial review).
Shown on the index list ("never" until first sync) and the detail page.
This is the shared field the "Auto-advance `start_date`" and "Show pending
counts" items build on.

**Completed:** 2026-07

### Auto-guess the currency from the account name
**(Features)**

Done (2026-07): if the selected account's name contains a known currency
code ("Chequing USD"), the form preselects it as the original currency (on
load and on account/budget change). A currency the user picked by hand is
never overridden. Client-side only; verified by driving the rendered
script in Chromium (not covered by pytest).

**Completed:** 2026-07

### Don't block the event loop with sync YNAB calls in async handlers
**(Correctness & robustness)**

Done (2026-07): `apply()`'s `get_transactions` + `update_transactions`
calls (and `mark_synced`) are wrapped in `await run_in_threadpool(...)`, so
a slow YNAB round-trip no longer stalls every other user's request. This
also made the `_apply_lock` genuinely load-bearing (the I/O now yields
inside the locked section). The other handlers are sync `def` and already
run in FastAPI's threadpool.

**Completed:** 2026-07

### One conversion per account
**(Correctness & robustness)**

Done: the new/edit forms disable accounts that already have a conversion,
and create/edit reject duplicates server-side with a 409.

**Update (v0.2.0.0, 2026-07-07):** that check alone was a check-then-insert race — two
concurrent requests (a double-submitted batch-create, or a batch racing a
single create/edit) could both pass it before either committed, producing
two conversions for one account. Worse, since `apply()`'s lock is keyed by
`conversion_id` not `account_id`, applying both duplicates around the same
time could race to PATCH the same real YNAB transaction with different
amounts. Closed with a DB-level `UNIQUE INDEX` on `(user_id, account_id)`
(`db._dedupe_and_index_conversions`), which also does a one-time cleanup of
any pre-existing duplicates on the live DB (keeps the older row) before the
index is created — see CLAUDE.md's "Schema changes need a migration" note
and "One conversion per account, enforced at the DB level" for the pattern.

**Completed:** 2026-07

### Friendly error pages
**(Correctness & robustness)**

Done: exception handlers in `main.py` render `error.html` (502) for
`YNABError`/`RatesError`, including connection failures, with a hint and
retry/back links.

**Completed:** 2026-07

### Handle YNAB rate limiting (429)
**(Correctness & robustness)**

Done: 429s render their own page explaining the ~200 req/hour budget.
Still worth adding YNAB delta requests (`last_knowledge_of_server`) to cut
request volume when the scheduler / pending-count badges land — noted
under those Features items.

**Completed:** 2026-07

### Zero-decimal display bug in preview
**(Correctness & robustness)**

Done: the converted column formats via `format_amount(new_milliunits,
to_currency)`.

**Completed:** 2026-07

### Validate currency direction on create
**(Correctness & robustness)**

Done: create and edit fetch the budget's `iso_code` from YNAB and reject
mismatches with a 400.

**Completed:** 2026-07

### Retries/backoff on outbound calls
**(Correctness & robustness)**

Done: `app/http.py` `get_with_retry` — one retry after 0.5s on connection
errors and 502/503/504, GETs only (the YNAB PATCH is never retried).

**Completed:** 2026-07

### CSRF tokens on POST forms
**(Security)**

Done: per-session token via `csrf_input()` in every form + `verify_csrf`
router dependency (403 on mismatch). Session cookie stays `SameSite=Lax`
as a second layer.

**Completed:** 2026-07

### Login rate limiting
**(Security)**

Done: in-memory counter in `auth.py`; after 5 consecutive failures each
further failure doubles the lockout (cap 5 min), 429 with a countdown
message meanwhile.

**Completed:** 2026-07

### Security headers
**(Security)**

Done: middleware sets X-Frame-Options, X-Content-Type-Options,
Referrer-Policy, and a CSP (inline scripts allowed — the templates use
small inline scripts, no external assets).

**Completed:** 2026-07

### Run the container as non-root
**(Security)**

Done: uvicorn runs as uid 1000 (matches the first user on a stock Debian
host so the bind-mounted `./data` stays writable; DEPLOY.md documents the
`chown` fallback).

**Completed:** 2026-07

### Public landing / home page
**(UX)**

Done: `/` now renders `landing.html` (pitch, three steps, privacy note,
log-in button) for anonymous visitors and redirects straight to
`/conversions` when logged in. No screenshot yet — add one if the page
ever needs selling power.

**Completed:** 2026-07

### Mobile-friendly styling
**(UX)**

First pass done: below 700px the memo column is hidden, padding/font
shrink, tables scroll horizontally, and the landing steps stack. Revisit a
card layout only if that isn't comfortable enough in practice.

**Completed:** 2026-07

### Totals row in preview
**(UX)**

Done: table footer with both sums (covers all listed rows; unticking
doesn't recompute — it's a sanity check).

**Completed:** 2026-07

### Post-apply flash instead of a bare page
**(UX)**

Done: apply redirects to the detail page with `?applied=N` and a flash;
`applied.html` removed.

**Completed:** 2026-07

### Dark mode
**(UX)**

Done (`prefers-color-scheme` variables in `style.css`).

**Completed:** 2026-07

### Sort the conversions list
**(UX)**

Done (2026-07): clickable column headers on the index
(`?sort=account|plan|currency|start|synced&order=asc|desc`) with an
active-direction arrow; server-side sort in the index route
(`_SORT_KEYS`). No sort param keeps the default insertion order. Pending
count isn't a column yet, so it's not a sort key.

**Completed:** 2026-07

### Collapse the plan column when there's only one plan
**(UX)**

Done (2026-07): the index hides the Plan column when every conversion
shares a `budget_name` (`single_plan`) and shows a "All conversions are in
‹plan›" caption instead; the column (and its sort header) return as soon as
a second plan appears.

**Completed:** 2026-07

### Bulk-delete via row checkboxes instead of a per-row Delete button
**(UX)**

Done (2026-07): the index row Delete button is replaced by a checkbox per
row (plus a select-all) and a "Delete selected" action posting to
`/conversions/bulk-delete` (single-transaction, CSRF-protected, JS
confirm, capped at 200 ids per request to prevent an unbounded-id-list
self-DoS — caught via adversarial review). The button stays disabled until
a row is checked. The per-conversion Delete on the detail page is
unchanged.

**Completed:** 2026-07

### Confirm password on the sign-up page
**(UX)**

Done (2026-07): second password field on `/signup`, checked client-side
(`setCustomValidity`) and server-side (400 on mismatch, before the user
row is created).

**Completed:** 2026-07

### Dependency updates
**(Ops / deployment)**

Done: `.github/dependabot.yml` (pip + GitHub Actions, weekly); CI green =
auto-deployable.

**Completed:** 2026-07

### Expose the running version
**(Ops / deployment)**

Done: `GET /healthz` (unauthenticated) returns `{status, version}` with
the git SHA baked in at build time (Dockerfile `ARG GIT_SHA`, exported by
`autodeploy.sh`); the page footer shows it too, and autodeploy verifies
the live version matches the deployed SHA. The compose file gained a
`healthcheck:` on it.

**Superseded 2026-07:** `version` now reports the release `VERSION` file
(gstack's `MAJOR.MINOR.PATCH.MICRO` scheme) instead of the git SHA; the SHA
moved to an internal `git_sha` image label that `autodeploy.sh` checks for
exact-commit deploy verification.

**Completed:** 2026-07

### Add lint + type-check to CI
**(Code health / CI)**

Done: `ruff check` (E/F/W/I/UP/B, line length 100) + `mypy` before
pytest. `ruff format --check` was considered and skipped — it fights the
compact literal style (test fixtures especially) for no correctness
benefit.

**Completed:** 2026-07

### Test coverage for the gaps above
**(Code health / CI)**

Done for everything fixed so far: split skipping, YNAB/Frankfurter error
paths, retries, 429, zero-decimal display, CSRF, throttling
(`tests/test_errors.py` and additions to the flow/convert tests).

**Completed:** 2026-07

### Async or pooled HTTP clients
**(Code health / CI)**

Done (the "pick one lifecycle" half): both clients are now cached
process-wide singletons with pooled connections. Going async remains a
scheduler-time decision.

**Completed:** 2026-07

---

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
