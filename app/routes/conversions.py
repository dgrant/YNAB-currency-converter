import asyncio
import re
from datetime import date, datetime, timezone

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData

from .. import oauth
from ..auth import require_login
from ..config import get_settings
from ..connections import ConnectionStore
from ..convert import (
    build_preview,
    decimal_digits,
    format_amount,
    format_original,
    is_excluded,
    is_skipped,
    is_split,
    pending_count,
)
from ..rates import FrankfurterClient, RatesError
from ..store import ConversionStore, DuplicateAccountError
from ..templates import templates
from ..users import User
from ..ynab import YNABClient, YNABError

# On-load pending-count refresh (opt-in, default off): only refresh a
# conversion whose cached count is older than this, and only the few most-stale
# per load — the guardrails behind revised premise 5 so a page view can't spend
# the YNAB budget or exhaust the single worker's threadpool. See index().
_ONLOAD_STALE_SECONDS = 3600
_ONLOAD_REFRESH_MAX = 2

router = APIRouter(dependencies=[Depends(require_login)])

# The Frankfurter client is a process-wide singleton so connections are pooled
# across requests; YNABClient shares one pooled httpx client the same way but
# carries each user's token per request (tests reset both; see conftest.py).
_rates_client: FrankfurterClient | None = None


def get_store() -> ConversionStore:
    return ConversionStore(get_settings().data_dir)


def require_ynab(user: User = Depends(require_login)) -> YNABClient:
    """The user's YNAB client, or a 303 to /settings if not connected yet."""
    settings = get_settings()
    store = ConnectionStore(settings.data_dir)
    # A connection with no refresh_token predates OAuth-only support; if
    # get_access_token is about to clean it up, tell /settings so it can
    # explain the forced reconnect instead of just saying "Not connected".
    existing = store.get(user.id)
    had_stale_connection = existing is not None and not existing.refresh_token
    token = oauth.get_access_token(settings, store, user.id)
    if token is None:
        location = "/settings?error=reauth" if had_stale_connection else "/settings"
        raise HTTPException(status_code=303, headers={"Location": location})
    return YNABClient(token, settings.ynab_api_base)


def get_rates_client() -> FrankfurterClient:
    global _rates_client
    if _rates_client is None:
        _rates_client = FrankfurterClient(get_settings().frankfurter_api_base)
    return _rates_client


# Sort keys for the conversions list, mapped to a stable sort function. Missing
# last_synced sorts as empty string (so never-synced rows group together); a
# missing pending_count sorts as 0 (never-checked groups with the caught-up).
_SORT_KEYS = {
    "account": lambda c: (c["account_name"] or "").lower(),
    "plan": lambda c: (c["budget_name"] or "").lower(),
    "currency": lambda c: (c["from_currency"], c["to_currency"]),
    "start": lambda c: c["start_date"],
    "synced": lambda c: c["last_synced"] or "",
    "pending": lambda c: c["pending_count"] or 0,
}


def _relative_time(iso: str | None, now: datetime) -> str | None:
    """A short 'checked N ago' for the badge staleness note; None if unknown."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (now - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _is_stale(checked_at: str | None, now: datetime) -> bool:
    if not checked_at:
        return True
    try:
        dt = datetime.fromisoformat(checked_at)
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() > _ONLOAD_STALE_SECONDS


def _maybe_onload_refresh(user: User, conversions: list[dict]) -> bool:
    """Opt-in (user.refresh_on_load), best-effort refresh of the most-stale
    pending counts before the dashboard renders. Bounded three ways so a page
    view can never spend the YNAB budget or pin the single worker's threadpool:
    only stale conversions (older than _ONLOAD_STALE_SECONDS), only the
    _ONLOAD_REFRESH_MAX most-stale, and on a short-timeout client with every
    error swallowed. Returns True if it wrote anything (caller re-loads)."""
    if not user.refresh_on_load:
        return False
    settings = get_settings()
    conn_store = ConnectionStore(settings.data_dir)
    if conn_store.get(user.id) is None:
        return False
    now = datetime.now(timezone.utc)
    stale = [c for c in conversions if _is_stale(c.get("pending_checked_at"), now)]
    if not stale:
        return False
    stale.sort(key=lambda c: c.get("pending_checked_at") or "")  # never-checked first
    # get_access_token can raise YNABError on a transient token-endpoint outage
    # (oauth._token_request re-raises 5xx rather than deleting the grant). That
    # must NOT turn a plain dashboard GET into a 502 for an opted-in user, so it
    # is inside the best-effort guard along with the fetch loop.
    try:
        token = oauth.get_access_token(settings, conn_store, user.id)
        if token is None:
            return False
        wrote = False
        with httpx.Client(base_url=settings.ynab_api_base, timeout=6) as client:
            ynab = YNABClient(token, settings.ynab_api_base, client=client)
            for conversion in stale[:_ONLOAD_REFRESH_MAX]:
                try:
                    txns = ynab.get_transactions(
                        conversion["budget_id"],
                        conversion["account_id"],
                        conversion["start_date"],
                    )
                except Exception:
                    continue  # one slow/failed account must not block the rest
                get_store().set_pending(
                    user.id, conversion["id"], pending_count(txns), _utcnow_iso()
                )
                wrote = True
        return wrote
    except Exception:
        # Any refresh failure (token outage, network) leaves cached counts in
        # place and renders the page — the docstring's best-effort promise.
        return False


@router.get("/conversions")
def index(
    request: Request,
    user: User = Depends(require_login),
    sort: str = "",
    order: str = "asc",
    created: int | None = None,
):
    settings = get_settings()
    has_ynab = ConnectionStore(settings.data_dir).get(user.id) is not None
    conversions = get_store().load(user.id)
    if _maybe_onload_refresh(user, conversions):
        conversions = get_store().load(user.id)
    if sort in _SORT_KEYS:
        conversions.sort(key=_SORT_KEYS[sort], reverse=(order == "desc"))
    else:
        # Dashboard default: accounts with pending work float to the top. A
        # stable sort on a constant key (all never-checked = 0) preserves
        # insertion order, so this is a no-op until counts exist.
        conversions.sort(key=lambda c: c["pending_count"] or 0, reverse=True)
    # The plan column is noise when every conversion lives in the same plan.
    single_plan = len({c["budget_name"] for c in conversions}) <= 1
    now = datetime.now(timezone.utc)
    for c in conversions:
        c["pending_checked_ago"] = _relative_time(c.get("pending_checked_at"), now)
    total_pending = sum(c["pending_count"] or 0 for c in conversions)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "conversions": conversions,
            "has_ynab": has_ynab,
            "sort": sort if sort in _SORT_KEYS else "",
            "order": "desc" if order == "desc" else "asc",
            "single_plan": single_plan,
            "created": created,
            "total_pending": total_pending,
            # all_checked drives the "Nothing pending" disabled state: only
            # trust total_pending==0 when every conversion has actually been
            # checked, so a never-checked account with real work can't hide
            # behind another account's 0.
            "all_checked": all(c["pending_checked_at"] for c in conversions),
            "refresh_on_load": user.refresh_on_load,
            "apply_summary": request.session.pop("apply_all_summary", None),
        },
    )


def _plan_currency(budget: dict) -> str:
    """A budget's own currency code as YNAB reports it, or "" if unset."""
    return (budget.get("currency_format") or {}).get("iso_code", "")


def _form_context(ynab: YNABClient) -> dict:
    """Budgets/accounts/currencies needed by the new & edit conversion forms."""
    budgets = []
    for budget in ynab.get_budgets():
        budgets.append(
            {
                "id": budget["id"],
                "name": budget["name"],
                "currency": _plan_currency(budget),
                "accounts": [
                    {"id": a["id"], "name": a["name"]}
                    for a in ynab.get_accounts(budget["id"])
                ],
            }
        )
    currencies = get_rates_client().currencies()
    return {"budgets": budgets, "currencies": currencies, "today": date.today().isoformat()}


def _used_account_ids(user_id: str, except_conversion_id: str | None = None) -> list[str]:
    """Account ids that already have a conversion (an account gets at most one)."""
    return sorted(
        c["account_id"]
        for c in get_store().load(user_id)
        if c["id"] != except_conversion_id
    )


def _reject_duplicate_account(
    user_id: str, account_id: str, except_conversion_id: str | None = None
) -> None:
    if account_id in _used_account_ids(user_id, except_conversion_id):
        raise HTTPException(409, "That account already has a conversion configured")


def _guess_currency(account_name: str, codes: set[str]) -> str:
    """Server-side twin of the new-form JS guess: if the account name carries a
    known currency code ("Chequing USD"), offer it as the default. Empty when
    there's no match — the user picks in that case."""
    for word in re.split(r"[^A-Za-z]+", account_name.upper()):
        if word in codes:
            return word
    return ""


def _account_index(ynab: YNABClient) -> dict[str, dict]:
    """Every account across every plan, keyed by its (globally unique) YNAB id,
    with the plan it belongs to and that plan's derived target currency. Used by
    batch-create to resolve names/currency from YNAB rather than the form."""
    index: dict[str, dict] = {}
    for budget in ynab.get_budgets():
        to_currency = _plan_currency(budget)
        for account in ynab.get_accounts(budget["id"]):
            index[account["id"]] = {
                "budget_id": budget["id"],
                "budget_name": budget["name"],
                "account_name": account["name"],
                "to_currency": to_currency,
            }
    return index


def _budget_currency(ynab: YNABClient, budget_id: str) -> str:
    """The plan's own currency, read straight from YNAB. This is the conversion
    target — deriving it (rather than trusting a form field) removes the whole
    class of user-picked-vs-actual mismatch."""
    budget = next((b for b in ynab.get_budgets() if b["id"] == budget_id), None)
    if budget is None:
        raise HTTPException(400, "Unknown plan")
    currency = _plan_currency(budget)
    if not currency:
        raise HTTPException(400, f"Plan '{budget['name']}' has no currency set in YNAB")
    return currency


@router.get("/conversions/new")
def new_form(
    request: Request,
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
):
    return templates.TemplateResponse(
        request,
        "conversion_form.html",
        {
            **_form_context(ynab),
            "conversion": None,
            "used_account_ids": _used_account_ids(user.id),
        },
    )


@router.post("/conversions")
def create(
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
    budget_id: str = Form(...),
    budget_name: str = Form(...),
    account_id: str = Form(...),
    account_name: str = Form(...),
    from_currency: str = Form(...),
    start_date: str = Form(...),
):
    _validate_start_date(start_date)
    _reject_duplicate_account(user.id, account_id)
    to_currency = _budget_currency(ynab, budget_id)
    try:
        conversion = get_store().add(
            user.id,
            {
                "budget_id": budget_id,
                "budget_name": budget_name,
                "account_id": account_id,
                "account_name": account_name,
                "from_currency": from_currency.upper(),
                "to_currency": to_currency,
                "start_date": start_date,
            },
        )
    except DuplicateAccountError as exc:
        # The pre-check above just lost a race to a concurrent request for
        # the same account — the DB's unique constraint is the real backstop.
        raise HTTPException(409, "That account already has a conversion configured") from exc
    return RedirectResponse(f"/conversions/{conversion['id']}", status_code=303)


@router.get("/conversions/batch")
def batch_form(
    request: Request,
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
):
    """One-shot setup: every not-yet-configured account across all plans, so a
    user with many foreign-currency accounts can create conversions at once."""
    used = set(_used_account_ids(user.id))
    context = _form_context(ynab)
    codes = set(context["currencies"])
    plans = []
    for budget in context["budgets"]:
        # Exclude the plan's own currency from the guess pool: an account
        # named e.g. "USD Checking" inside a USD plan should never be
        # auto-guessed as a same-currency "conversion" (rate ~1, a silent
        # no-op) — leave the field blank so the user picks deliberately.
        guess_codes = codes - {budget["currency"]}
        accounts = [
            {**account, "guess": _guess_currency(account["name"], guess_codes)}
            for account in budget["accounts"]
            if account["id"] not in used
        ]
        # A plan with no derivable currency can't be a conversion target; drop
        # its accounts rather than offer a broken row (mirrors create's 400).
        if accounts and budget["currency"]:
            plans.append({**budget, "accounts": accounts})
    return templates.TemplateResponse(
        request,
        "batch_form.html",
        {"plans": plans, "currencies": context["currencies"], "today": context["today"]},
    )


# Same rationale as _MAX_BULK_DELETE below: bounds an attacker-supplied
# `create` list to a small, real-world-sized batch instead of unbounded work.
_MAX_BATCH_CREATE = 200


def _create_batch(
    user_id: str, selected: list[str], form: FormData, accounts: dict[str, dict]
) -> int:
    """Sync body of batch_create's insert loop, run off the event loop via
    run_in_threadpool (batch_create is async def for the awaits around it, but
    sqlite3 writes are blocking — same reason apply() offloads its DB/YNAB
    calls). form is Starlette's FormData; read-only here, so sharing it with
    the event loop thread is safe."""
    used = set(_used_account_ids(user_id))
    to_create = []
    for account_id in selected:
        info = accounts.get(account_id)
        # Skip anything unknown, already configured, or (defensively) selected
        # twice — don't fail the whole batch over one bad row.
        if info is None or account_id in used or not info["to_currency"]:
            continue
        start_date = str(form.get(f"start_{account_id}", ""))
        try:
            date.fromisoformat(start_date)
        except ValueError:
            continue  # malformed/tampered date: skip this row, not the batch
        from_currency = str(form.get(f"from_{account_id}", "")).upper()
        if not from_currency:
            continue
        to_create.append(
            {
                "budget_id": info["budget_id"],
                "budget_name": info["budget_name"],
                "account_id": account_id,
                "account_name": info["account_name"],
                "from_currency": from_currency,
                "to_currency": info["to_currency"],
                "start_date": start_date,
            }
        )
        used.add(account_id)
    return len(get_store().add_many(user_id, to_create))


@router.post("/conversions/batch")
async def batch_create(
    request: Request,
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
):
    form = await request.form()
    selected = [str(a) for a in form.getlist("create")]
    if not selected:
        return RedirectResponse("/conversions", status_code=303)
    if len(selected) > _MAX_BATCH_CREATE:
        raise HTTPException(400, f"Too many accounts selected (max {_MAX_BATCH_CREATE})")
    # Resolve plan/name/currency from YNAB, not the form, so those can't be
    # tampered with (same reason to_currency is derived, not posted).
    accounts = await run_in_threadpool(_account_index, ynab)
    created = await run_in_threadpool(_create_batch, user.id, selected, form, accounts)
    return RedirectResponse(f"/conversions?created={created}", status_code=303)


# One asyncio.Lock per conversion, guarding apply's fetch→filter→PATCH section.
# Lazily created on the single event loop, so a plain dict is safe.
_apply_locks: dict[str, asyncio.Lock] = {}


def _apply_lock(conversion_id: str) -> asyncio.Lock:
    lock = _apply_locks.get(conversion_id)
    if lock is None:
        lock = _apply_locks[conversion_id] = asyncio.Lock()
    return lock


def _memo_from_form(value: object) -> str:
    """Memo from a preview form field. Browsers normalize newlines in form
    values to CRLF on submit, which can push a server-built <=500-byte memo
    past the cap — undo that before the [:500] backstop so the trailing FX/skip
    marker (which future previews rely on) can't get sliced off."""
    return str(value).replace("\r\n", "\n")[:500]


def _get_conversion_or_404(user_id: str, conversion_id: str) -> dict:
    conversion = get_store().get(user_id, conversion_id)
    if conversion is None:
        raise HTTPException(404, "Conversion not found")
    return conversion


def _validate_start_date(start_date: str) -> None:
    """A tampered/malformed start_date is a 400, not an unhandled 500."""
    try:
        date.fromisoformat(start_date)
    except ValueError as exc:
        raise HTTPException(400, "start_date must be a valid YYYY-MM-DD date") from exc


@router.get("/conversions/{conversion_id}")
def detail(
    request: Request,
    conversion_id: str,
    user: User = Depends(require_login),
    applied: int | None = None,
    skipped_splits: int = 0,
):
    conversion = _get_conversion_or_404(user.id, conversion_id)
    return templates.TemplateResponse(
        request,
        "detail.html",
        {"conversion": conversion, "applied": applied, "skipped_splits": skipped_splits},
    )


@router.get("/conversions/{conversion_id}/edit")
def edit_form(
    request: Request,
    conversion_id: str,
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
):
    conversion = _get_conversion_or_404(user.id, conversion_id)
    return templates.TemplateResponse(
        request,
        "conversion_form.html",
        {
            **_form_context(ynab),
            "conversion": conversion,
            "used_account_ids": _used_account_ids(user.id, except_conversion_id=conversion_id),
        },
    )


@router.post("/conversions/{conversion_id}/edit")
def edit(
    conversion_id: str,
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
    budget_id: str = Form(...),
    budget_name: str = Form(...),
    account_id: str = Form(...),
    account_name: str = Form(...),
    from_currency: str = Form(...),
    start_date: str = Form(...),
):
    _validate_start_date(start_date)
    _get_conversion_or_404(user.id, conversion_id)
    _reject_duplicate_account(user.id, account_id, except_conversion_id=conversion_id)
    to_currency = _budget_currency(ynab, budget_id)
    try:
        get_store().update(
            user.id,
            conversion_id,
            {
                "budget_id": budget_id,
                "budget_name": budget_name,
                "account_id": account_id,
                "account_name": account_name,
                "from_currency": from_currency.upper(),
                "to_currency": to_currency,
                "start_date": start_date,
            },
        )
    except DuplicateAccountError as exc:
        raise HTTPException(409, "That account already has a conversion configured") from exc
    return RedirectResponse(f"/conversions/{conversion_id}", status_code=303)


# Real accounts have at most a handful of conversions (one per YNAB account).
# This cap exists only to stop an attacker-supplied `ids` list from turning
# one request into an oversized query that ties up a threadpool slot — this
# app is a single uvicorn worker with a small shared threadpool.
_MAX_BULK_DELETE = 200


@router.post("/conversions/bulk-delete")
def bulk_delete(
    user: User = Depends(require_login),
    ids: list[str] = Form(default=[]),
):
    """Delete several conversions at once from the index page's row checkboxes.
    One connection/transaction, scoped by user_id, so any id not owned by the
    user is silently skipped."""
    if len(ids) > _MAX_BULK_DELETE:
        raise HTTPException(400, f"Too many conversions selected (max {_MAX_BULK_DELETE})")
    get_store().delete_many(user.id, ids)
    return RedirectResponse("/conversions", status_code=303)


@router.post("/conversions/{conversion_id}/delete")
def delete(conversion_id: str, user: User = Depends(require_login)):
    get_store().delete(user.id, conversion_id)
    return RedirectResponse("/conversions", status_code=303)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_group(ynab: YNABClient, conversion: dict) -> dict:
    """Fetch one conversion's transactions and compute its preview rows +
    pending count. Raises YNABError/RatesError on upstream failure — callers
    decide whether that aborts (single preview) or fails just this group
    (preview-all). No store writes here: the caller marks synced / sets the
    badge only after this returns, so a failure never leaves a false 'synced'."""
    transactions = ynab.get_transactions(
        conversion["budget_id"], conversion["account_id"], conversion["start_date"]
    )
    pending, skipped_splits = [], 0
    skipped_marked = [
        {"payee_name": t.get("payee_name") or "", "date": t["date"]}
        for t in transactions
        if is_skipped(t.get("memo"))
    ]
    for txn in transactions:
        if is_excluded(txn):
            continue
        if is_split(txn):
            skipped_splits += 1
        else:
            pending.append(txn)
    rows = []
    if pending:
        dates = [date.fromisoformat(t["date"]) for t in pending]
        rates = get_rates_client().get_rates(
            conversion["from_currency"], conversion["to_currency"], min(dates), max(dates)
        )
        rows = build_preview(
            pending, rates, conversion["from_currency"], conversion["to_currency"]
        )
    totals = None
    if rows:
        totals = {
            "original": format_original(
                sum(r["original_milliunits"] for r in rows), conversion["from_currency"]
            ),
            "converted": format_amount(
                sum(r["new_milliunits"] for r in rows), conversion["to_currency"]
            ),
        }
    return {
        "conversion": conversion,
        "rows": rows,
        "totals": totals,
        "skipped_splits": skipped_splits,
        "skipped_marked": skipped_marked,
        "total_fetched": len(transactions),
        # is_convertible == not-excluded and not-split, so this equals len(pending);
        # go through pending_count() to keep one definition for the badge.
        "pending_count": pending_count(transactions),
        "from_digits": decimal_digits(conversion["from_currency"]),
        "to_digits": decimal_digits(conversion["to_currency"]),
        "error": None,
    }


def _parse_updates(form: FormData, txn_ids: list[str]) -> tuple[list[dict], dict]:
    """Turn the preview form's per-row hidden fields into YNAB update dicts +
    the metadata (action, pre-preview amount) the write-time re-checks need.
    Raises KeyError/ValueError on a malformed/tampered form (caught by callers
    as a 400 / per-group error). Shared by single apply and apply-all."""
    updates: list[dict] = []
    meta: dict[str, dict] = {}
    for txn_id in txn_ids:
        # Required: a missing action must not silently default to convert
        # (the one action that rewrites the amount) — KeyError -> 400.
        action = str(form[f"action_{txn_id}"])
        original = int(str(form[f"original_{txn_id}"]))
        if action == "convert":
            updates.append(
                {
                    "id": txn_id,
                    "amount": int(str(form[f"amount_{txn_id}"])),
                    "memo": _memo_from_form(form[f"memo_{txn_id}"]),
                }
            )
        elif action == "already":
            updates.append(
                {"id": txn_id, "memo": _memo_from_form(form[f"already_memo_{txn_id}"])}
            )
        elif action == "skip":
            updates.append(
                {"id": txn_id, "memo": _memo_from_form(form[f"skip_memo_{txn_id}"])}
            )
        else:
            raise ValueError(f"unknown action {action!r}")
        meta[txn_id] = {"action": action, "original": original}
    return updates, meta


async def _apply_updates(
    user_id: str, ynab: YNABClient, conversion: dict, updates: list[dict], meta: dict
) -> dict:
    """Locked fetch -> re-check -> PATCH for ONE conversion, then advance its
    last_synced / badge / start_date. The re-checks (present/split/stale/edited)
    run against THIS conversion's own re-fetched account — never a shared
    present_ids union, so a txn posted under the wrong group can't validate.
    Returns {applied, skipped_splits, dropped}. Raises YNABError on a failed
    fetch/PATCH — the caller decides (single apply re-raises to the handler;
    apply-all treats non-401/429 as a per-group failure)."""
    conversion_id = conversion["id"]
    updated: list[dict] = []
    skipped_splits = 0
    if not updates:
        return {"applied": [], "skipped_splits": 0, "dropped": 0}
    async with _apply_lock(conversion_id):
        # Re-read start_date under the lock; a concurrent apply may have just
        # advanced it, and comparing against a pre-lock snapshot could regress
        # the floor. Falls back to the passed snapshot if the row vanished.
        current_conversion = (
            await run_in_threadpool(get_store().get, user_id, conversion_id) or conversion
        )
        current = await run_in_threadpool(
            ynab.get_transactions,
            conversion["budget_id"],
            conversion["account_id"],
            current_conversion["start_date"],
        )
        current_by_id = {t["id"]: t for t in current}
        present_ids = set(current_by_id)
        split_ids = {tid for tid, t in current_by_id.items() if is_split(t)}
        stale_ids = {tid for tid, t in current_by_id.items() if is_excluded(t)}
        edited_ids = {
            tid
            for tid, m in meta.items()
            if tid in current_by_id
            and m["action"] in ("convert", "already")
            and current_by_id[tid]["amount"] != m["original"]
        }
        safe = [
            u
            for u in updates
            if u["id"] in present_ids
            and u["id"] not in split_ids
            and u["id"] not in stale_ids
            and u["id"] not in edited_ids
        ]
        skipped_splits = sum(1 for u in updates if u["id"] in split_ids)
        if safe:
            updated = await run_in_threadpool(
                ynab.update_transactions, conversion["budget_id"], safe
            )
        # last_synced + badge only after the fetch (and PATCH, if any) succeeded.
        now_date = date.today().isoformat()
        await run_in_threadpool(get_store().mark_synced, user_id, conversion_id, now_date)
        applied_ids = {t["id"] for t in updated}
        await run_in_threadpool(
            get_store().set_pending,
            user_id,
            conversion_id,
            pending_count(current, applied_ids),
            _utcnow_iso(),
        )
        if safe:
            # Advance the fetch floor from YNAB's CONFIRMED response (`updated`),
            # never the request — so a partial confirm can't skip a txn that
            # was never actually written.
            pending_dates = [
                t["date"]
                for t in current
                if not is_excluded(t) and t["id"] not in applied_ids
            ]
            new_start = min(pending_dates) if pending_dates else date.today().isoformat()
            if new_start > current_conversion["start_date"]:
                await run_in_threadpool(
                    get_store().set_start_date, user_id, conversion_id, new_start
                )
    dropped = len(updates) - len(updated)
    return {"applied": updated, "skipped_splits": skipped_splits, "dropped": dropped}


@router.post("/conversions/{conversion_id}/preview")
def preview(
    request: Request,
    conversion_id: str,
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
):
    conversion = _get_conversion_or_404(user.id, conversion_id)
    group = _build_group(ynab, conversion)
    # Only mark synced / refresh the badge once the preview actually succeeded
    # — _build_group raises before returning if the fetch or rates call failed,
    # so we never claim "synced" for a cycle that didn't complete.
    get_store().mark_synced(user.id, conversion_id, date.today().isoformat())
    get_store().set_pending(user.id, conversion_id, group["pending_count"], _utcnow_iso())
    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            "conversion": conversion,
            "rows": group["rows"],
            "totals": group["totals"],
            "skipped_splits": group["skipped_splits"],
            "skipped_marked": group["skipped_marked"],
            "total_fetched": group["total_fetched"],
            "from_digits": group["from_digits"],
            "to_digits": group["to_digits"],
        },
    )


@router.post("/conversions/preview-all")
def preview_all(
    request: Request,
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
):
    """Preview every configured conversion in one grouped page. Per-group
    upstream failures (a rate outage, a non-auth YNAB error) render as a failed
    group and don't blank the rest; a 401 (revoked token) or 429 (budget
    exhausted) re-raises to the global handler — 401 reconnects, 429 stops the
    loop instead of firing N more requests into a rate-limited API."""
    conversions = get_store().load(user.id)
    if not conversions:
        return RedirectResponse("/conversions", status_code=303)
    groups = []
    now_date = date.today().isoformat()
    checked_at = _utcnow_iso()
    for conversion in conversions:
        try:
            group = _build_group(ynab, conversion)
        except YNABError as exc:
            if exc.status_code in (401, 429):
                raise
            groups.append(_failed_group(conversion, str(exc)))
            continue
        except RatesError as exc:
            groups.append(_failed_group(conversion, str(exc)))
            continue
        get_store().mark_synced(user.id, conversion["id"], now_date)
        get_store().set_pending(user.id, conversion["id"], group["pending_count"], checked_at)
        groups.append(group)
    total_pending = sum(g["pending_count"] for g in groups if g["error"] is None)
    return templates.TemplateResponse(
        request,
        "preview_all.html",
        {"groups": groups, "total_pending": total_pending},
    )


def _failed_group(conversion: dict, error: str) -> dict:
    return {
        "conversion": conversion,
        "rows": [],
        "totals": None,
        "skipped_splits": 0,
        "skipped_marked": [],
        "total_fetched": 0,
        "pending_count": 0,
        "from_digits": decimal_digits(conversion["from_currency"]),
        "to_digits": decimal_digits(conversion["to_currency"]),
        "error": error,
    }


@router.post("/conversions/{conversion_id}/apply")
async def apply(
    request: Request,
    conversion_id: str,
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
):
    conversion = _get_conversion_or_404(user.id, conversion_id)
    form = await request.form()
    try:
        updates, meta = _parse_updates(form, [str(t) for t in form.getlist("selected")])
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            400, "Malformed apply form — go back and run the preview again"
        ) from exc
    # A YNABError here (401/429/other) propagates to the global handler — same
    # routing single-preview has always had (401 -> reconnect, 429 -> its page).
    result = await _apply_updates(user.id, ynab, conversion, updates, meta)
    skipped_splits = result["skipped_splits"]
    suffix = f"&skipped_splits={skipped_splits}" if skipped_splits else ""
    return RedirectResponse(
        f"/conversions/{conversion_id}?applied={len(result['applied'])}{suffix}",
        status_code=303,
    )


@router.post("/conversions/apply-all")
async def apply_all(
    request: Request,
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
):
    """Approve the combined preview: one conversion at a time, each inside its
    own lock. A per-group failure (bad form, non-auth YNAB error) is reported
    and the rest still run; a 401/429 re-raises (reconnect / stop hammering a
    rate-limited API). Each group's selected txns come from its own
    `selected_<conversion_id>` list, and its `conversion_id` is validated
    against the user's own conversions — never trusted blindly."""
    form = await request.form()
    conversion_ids = [str(c) for c in form.getlist("conversion_ids")]
    results = []
    for cid in conversion_ids:
        conversion = get_store().get(user.id, cid)
        if conversion is None:
            continue  # not owned by this user, or deleted since the preview
        selected = [str(t) for t in form.getlist(f"selected_{cid}")]
        try:
            updates, meta = _parse_updates(form, selected)
        except (KeyError, ValueError):
            results.append({"account_name": conversion["account_name"], "error": "malformed form"})
            continue
        try:
            result = await _apply_updates(user.id, ynab, conversion, updates, meta)
        except YNABError as exc:
            if exc.status_code in (401, 429):
                raise
            # Truncate: the summary is stored in the signed session cookie
            # (~4KB); a long YNAB message across many accounts could overflow
            # it and silently drop the whole flash.
            results.append(
                {"account_name": conversion["account_name"], "error": str(exc)[:120]}
            )
            continue
        results.append(
            {
                "account_name": conversion["account_name"],
                "applied": len(result["applied"]),
                "dropped": result["dropped"],
                "skipped_splits": result["skipped_splits"],
                "error": None,
            }
        )
    # Per-conversion summary can't fit a query param (see index()); stash it in
    # the session for the next render, then redirect to the dashboard.
    request.session["apply_all_summary"] = results
    return RedirectResponse("/conversions", status_code=303)
