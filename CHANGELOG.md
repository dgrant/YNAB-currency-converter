# Changelog

All notable changes to this project are documented here. Versions use gstack's
four-part `MAJOR.MINOR.PATCH.MICRO` scheme; the canonical version lives in the
root `VERSION` file. New entries go directly under this header, newest first.

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
