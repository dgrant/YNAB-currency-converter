# Changelog

All notable changes to this project are documented here. Versions use gstack's
four-part `MAJOR.MINOR.PATCH.MICRO` scheme; the canonical version lives in the
root `VERSION` file. New entries go directly under this header, newest first.

## [0.2.1.0] - 2026-07-08

### Changed
- Dependency management moved from pip + `requirements.txt` to
  [uv](https://docs.astral.sh/uv/). Runtime deps now live in
  `pyproject.toml`'s `[project.dependencies]`, dev tools in a `dev` dependency
  group, and exact versions are pinned in a committed `uv.lock`. The Dockerfile
  installs from the lockfile with `uv sync --frozen --no-dev`, CI uses
  `astral-sh/setup-uv` + `uv run`, and the dev docs describe the `uv sync` /
  `uv run` workflow. No runtime behavior changes.

## [0.2.0.1] - 2026-07-07

### Fixed
- Email input on the login and signup pages rendered with browser defaults
  (narrow, wrong font, no rounded corners) instead of the app's form styling,
  because the input style rule only matched `password`, `date`, and `select`.
  The rule now also covers `email`, `text`, and `number` inputs so every
  text-entry field looks consistent.

## [0.2.0.0] - 2026-07-07

### Added
- Batch-add conversions: a new "Batch add" flow lists every YNAB account you
  haven't set up yet, across all your plans, and lets you create conversions
  for several at once — with the original currency pre-guessed from each
  account's name — instead of adding them one at a time.

### Changed
- The plan currency to convert into is now read directly from YNAB instead
  of being a field you pick, so it can never disagree with your plan's actual
  currency. The new/edit conversion form shows it read-only.
- After you apply a batch of conversions, the conversion's start date now
  advances past everything that's been handled, so future previews stop
  refetching already-converted history as an account's history grows. It only
  ever moves forward, and never past a transaction still waiting to be
  converted (splits, unticked rows).

### Fixed
- "One conversion per account" is now enforced at the database level, closing
  a race where two near-simultaneous requests (a double-submitted batch, or a
  batch racing a single add) could create duplicate conversions for the same
  account — which, if both were applied at once, could overwrite each other's
  edit to the same YNAB transaction with a wrong amount. Any pre-existing
  duplicates are cleaned up automatically on startup, keeping the older one.

## [0.1.0.0] - 2026-07-07

Baseline release — establishes versioning and a changelog for the existing
Currency Converter for YNAB app.

### Added
- Self-hosted conversions page for YNAB: enter transactions in an account's
  original foreign currency and have them converted to the budget's currency
  using the exchange rate of each transaction's date.
- Conversion flow — pick a budget, the foreign-currency account, and the
  currency; the app edits each transaction's amount in place and appends a
  memo like `-1,817 JPY (FX rate: 0.0087987)`.
- Idempotent conversions: the memo format matches ynab.rmillan.com, so
  transactions already converted (by this app or that service) are recognized
  and never converted twice.
- FastAPI web app with Docker/Docker Compose deployment (`Dockerfile`,
  `docker-compose.yml`, `deploy/`, `DEPLOY.md`).
- Developer tooling: pytest suite, ruff linting, and mypy type checking
  configured in `pyproject.toml`.
- `VERSION` file and this `CHANGELOG.md` following gstack's versioning and
  changelog conventions.

### Changed
- `/healthz` and the page footer now report the release `VERSION` instead
  of the deployed git SHA. The SHA still exists internally (cache-busting,
  and exact-commit deploy verification via a `git_sha` image label checked
  by `autodeploy.sh`), it's just no longer the user-facing "version."
