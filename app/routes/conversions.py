import asyncio
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

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
)
from ..rates import FrankfurterClient
from ..store import ConversionStore
from ..templates import templates
from ..users import User
from ..ynab import YNABClient

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


@router.get("/conversions")
def index(request: Request, user: User = Depends(require_login)):
    settings = get_settings()
    has_ynab = ConnectionStore(settings.data_dir).get(user.id) is not None
    return templates.TemplateResponse(
        request,
        "index.html",
        {"conversions": get_store().load(user.id), "has_ynab": has_ynab},
    )


def _form_context(ynab: YNABClient) -> dict:
    """Budgets/accounts/currencies needed by the new & edit conversion forms."""
    budgets = []
    for budget in ynab.get_budgets():
        budgets.append(
            {
                "id": budget["id"],
                "name": budget["name"],
                "currency": (budget.get("currency_format") or {}).get("iso_code", ""),
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


def _validate_to_currency(ynab: YNABClient, budget_id: str, to_currency: str) -> None:
    """The 'to' currency must be the budget's own currency — check YNAB rather
    than trusting the form field."""
    budget = next((b for b in ynab.get_budgets() if b["id"] == budget_id), None)
    if budget is None:
        raise HTTPException(400, "Unknown plan")
    budget_currency = (budget.get("currency_format") or {}).get("iso_code", "")
    if budget_currency and to_currency != budget_currency:
        raise HTTPException(
            400,
            f"Plan '{budget['name']}' uses {budget_currency}; transactions must be "
            f"converted to {budget_currency}, not {to_currency}",
        )


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
    to_currency: str = Form(...),
    start_date: str = Form(...),
):
    _validate_start_date(start_date)
    _reject_duplicate_account(user.id, account_id)
    _validate_to_currency(ynab, budget_id, to_currency.upper())
    conversion = get_store().add(
        user.id,
        {
            "budget_id": budget_id,
            "budget_name": budget_name,
            "account_id": account_id,
            "account_name": account_name,
            "from_currency": from_currency.upper(),
            "to_currency": to_currency.upper(),
            "start_date": start_date,
        },
    )
    return RedirectResponse(f"/conversions/{conversion['id']}", status_code=303)


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
    to_currency: str = Form(...),
    start_date: str = Form(...),
):
    _validate_start_date(start_date)
    _get_conversion_or_404(user.id, conversion_id)
    _reject_duplicate_account(user.id, account_id, except_conversion_id=conversion_id)
    _validate_to_currency(ynab, budget_id, to_currency.upper())
    get_store().update(
        user.id,
        conversion_id,
        {
            "budget_id": budget_id,
            "budget_name": budget_name,
            "account_id": account_id,
            "account_name": account_name,
            "from_currency": from_currency.upper(),
            "to_currency": to_currency.upper(),
            "start_date": start_date,
        },
    )
    return RedirectResponse(f"/conversions/{conversion_id}", status_code=303)


@router.post("/conversions/{conversion_id}/delete")
def delete(conversion_id: str, user: User = Depends(require_login)):
    get_store().delete(user.id, conversion_id)
    return RedirectResponse("/conversions", status_code=303)


@router.post("/conversions/{conversion_id}/preview")
def preview(
    request: Request,
    conversion_id: str,
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
):
    conversion = _get_conversion_or_404(user.id, conversion_id)
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
    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            "conversion": conversion,
            "rows": rows,
            "totals": totals,
            "skipped_splits": skipped_splits,
            "skipped_marked": skipped_marked,
            "total_fetched": len(transactions),
            "from_digits": decimal_digits(conversion["from_currency"]),
            "to_digits": decimal_digits(conversion["to_currency"]),
        },
    )


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
        updates = []
        # Per-txn metadata kept out of the PATCH body: the action and the
        # amount the preview was computed against, for write-time re-checks.
        meta: dict[str, dict] = {}
        for raw_txn_id in form.getlist("selected"):
            txn_id = str(raw_txn_id)
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
                # Amount is already in the budget currency — patch the memo only.
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
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            400, "Malformed apply form — go back and run the preview again"
        ) from exc
    updated, skipped_splits = [], 0
    if updates:
        # Serialize the fetch→filter→PATCH per conversion so two concurrent
        # approves (double-click, two tabs) can't both pass the re-checks
        # below against the same pre-PATCH state and race. (`ynab` is the
        # per-user client injected as a dependency.)
        # The YNAB client is synchronous; run its calls in the threadpool so a
        # slow round-trip doesn't stall the event loop (and with it every
        # other user's request). This also makes the lock do real work: with
        # the I/O yielding, two concurrent applies genuinely interleave.
        async with _apply_lock(conversion_id):
            current = await run_in_threadpool(
                ynab.get_transactions,
                conversion["budget_id"],
                conversion["account_id"],
                conversion["start_date"],
            )
            current_by_id = {t["id"]: t for t in current}
            present_ids = set(current_by_id)
            split_ids = {tid for tid, t in current_by_id.items() if is_split(t)}
            # Drop anything that got converted or skipped since the preview
            # (stale form, second tab, back-button resubmit) — its marker means
            # that decision already happened, and overwriting would clobber it.
            stale_ids = {tid for tid, t in current_by_id.items() if is_excluded(t)}
            # Drop rows whose amount was edited in YNAB since the preview: the
            # convert amount / "already" equivalence was computed against the
            # old amount, so writing it now would record a wrong value and the
            # marker would hide the mismatch from every future preview. (skip
            # is amount-independent — a bare "(skipped)" note stays valid.)
            edited_ids = {
                tid
                for tid, m in meta.items()
                if tid in current_by_id
                and m["action"] in ("convert", "already")
                and current_by_id[tid]["amount"] != m["original"]
            }
            # A txn deleted in YNAB between preview and approval would otherwise
            # make the whole bulk PATCH fail, applying nothing.
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
    suffix = f"&skipped_splits={skipped_splits}" if skipped_splits else ""
    return RedirectResponse(
        f"/conversions/{conversion_id}?applied={len(updated)}{suffix}", status_code=303
    )
