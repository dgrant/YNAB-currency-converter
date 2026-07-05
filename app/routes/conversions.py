from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..auth import require_login
from ..config import get_settings
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
from ..ynab import YNABClient

router = APIRouter(dependencies=[Depends(require_login)])

# Both HTTP clients are process-wide singletons so connections are pooled
# across requests (tests reset them; see conftest.py).
_rates_client: FrankfurterClient | None = None
_ynab_client: YNABClient | None = None


def get_store() -> ConversionStore:
    return ConversionStore(get_settings().data_dir)


def get_ynab() -> YNABClient:
    global _ynab_client
    if _ynab_client is None:
        settings = get_settings()
        if not settings.ynab_token:
            raise HTTPException(500, "YNAB_TOKEN is not configured")
        _ynab_client = YNABClient(settings.ynab_token, settings.ynab_api_base)
    return _ynab_client


def get_rates_client() -> FrankfurterClient:
    global _rates_client
    if _rates_client is None:
        _rates_client = FrankfurterClient(get_settings().frankfurter_api_base)
    return _rates_client


@router.get("/conversions")
def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html", {"conversions": get_store().load()}
    )


def _form_context() -> dict:
    """Budgets/accounts/currencies needed by the new & edit conversion forms."""
    ynab = get_ynab()
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


def _used_account_ids(except_conversion_id: str | None = None) -> list[str]:
    """Account ids that already have a conversion (an account gets at most one)."""
    return sorted(
        c["account_id"]
        for c in get_store().load()
        if c["id"] != except_conversion_id
    )


def _reject_duplicate_account(account_id: str, except_conversion_id: str | None = None) -> None:
    if account_id in _used_account_ids(except_conversion_id):
        raise HTTPException(409, "That account already has a conversion configured")


def _validate_to_currency(budget_id: str, to_currency: str) -> None:
    """The 'to' currency must be the budget's own currency — check YNAB rather
    than trusting the form field."""
    budget = next((b for b in get_ynab().get_budgets() if b["id"] == budget_id), None)
    if budget is None:
        raise HTTPException(400, "Unknown budget")
    budget_currency = (budget.get("currency_format") or {}).get("iso_code", "")
    if budget_currency and to_currency != budget_currency:
        raise HTTPException(
            400,
            f"Budget '{budget['name']}' uses {budget_currency}; transactions must be "
            f"converted to {budget_currency}, not {to_currency}",
        )


@router.get("/conversions/new")
def new_form(request: Request):
    return templates.TemplateResponse(
        request,
        "conversion_form.html",
        {**_form_context(), "conversion": None, "used_account_ids": _used_account_ids()},
    )


@router.post("/conversions")
def create(
    budget_id: str = Form(...),
    budget_name: str = Form(...),
    account_id: str = Form(...),
    account_name: str = Form(...),
    from_currency: str = Form(...),
    to_currency: str = Form(...),
    start_date: str = Form(...),
):
    date.fromisoformat(start_date)
    _reject_duplicate_account(account_id)
    _validate_to_currency(budget_id, to_currency.upper())
    conversion = get_store().add(
        {
            "budget_id": budget_id,
            "budget_name": budget_name,
            "account_id": account_id,
            "account_name": account_name,
            "from_currency": from_currency.upper(),
            "to_currency": to_currency.upper(),
            "start_date": start_date,
        }
    )
    return RedirectResponse(f"/conversions/{conversion['id']}", status_code=303)


def _get_conversion_or_404(conversion_id: str) -> dict:
    conversion = get_store().get(conversion_id)
    if conversion is None:
        raise HTTPException(404, "Conversion not found")
    return conversion


@router.get("/conversions/{conversion_id}")
def detail(
    request: Request,
    conversion_id: str,
    applied: int | None = None,
    skipped_splits: int = 0,
):
    conversion = _get_conversion_or_404(conversion_id)
    return templates.TemplateResponse(
        request,
        "detail.html",
        {"conversion": conversion, "applied": applied, "skipped_splits": skipped_splits},
    )


@router.get("/conversions/{conversion_id}/edit")
def edit_form(request: Request, conversion_id: str):
    conversion = _get_conversion_or_404(conversion_id)
    return templates.TemplateResponse(
        request,
        "conversion_form.html",
        {
            **_form_context(),
            "conversion": conversion,
            "used_account_ids": _used_account_ids(except_conversion_id=conversion_id),
        },
    )


@router.post("/conversions/{conversion_id}/edit")
def edit(
    conversion_id: str,
    budget_id: str = Form(...),
    budget_name: str = Form(...),
    account_id: str = Form(...),
    account_name: str = Form(...),
    from_currency: str = Form(...),
    to_currency: str = Form(...),
    start_date: str = Form(...),
):
    date.fromisoformat(start_date)
    _reject_duplicate_account(account_id, except_conversion_id=conversion_id)
    _validate_to_currency(budget_id, to_currency.upper())
    updated = get_store().update(
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
    if updated is None:
        raise HTTPException(404, "Conversion not found")
    return RedirectResponse(f"/conversions/{conversion_id}", status_code=303)


@router.post("/conversions/{conversion_id}/delete")
def delete(conversion_id: str):
    get_store().delete(conversion_id)
    return RedirectResponse("/conversions", status_code=303)


@router.post("/conversions/{conversion_id}/preview")
def preview(request: Request, conversion_id: str):
    conversion = _get_conversion_or_404(conversion_id)
    transactions = get_ynab().get_transactions(
        conversion["budget_id"], conversion["account_id"], conversion["start_date"]
    )
    pending, skipped_splits = [], 0
    skipped_marked = sum(1 for t in transactions if is_skipped(t.get("memo")))
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
async def apply(request: Request, conversion_id: str):
    conversion = _get_conversion_or_404(conversion_id)
    form = await request.form()
    try:
        updates = []
        for txn_id in form.getlist("selected"):
            # Required: a missing action must not silently default to convert
            # (the one action that rewrites the amount) — KeyError -> 400.
            action = str(form[f"action_{txn_id}"])
            if action == "convert":
                updates.append(
                    {
                        "id": txn_id,
                        "amount": int(str(form[f"amount_{txn_id}"])),
                        "memo": str(form[f"memo_{txn_id}"])[:500],
                    }
                )
            elif action == "already":
                # Amount is already in the budget currency — patch the memo only.
                updates.append(
                    {"id": txn_id, "memo": str(form[f"already_memo_{txn_id}"])[:500]}
                )
            elif action == "skip":
                updates.append(
                    {"id": txn_id, "memo": str(form[f"skip_memo_{txn_id}"])[:500]}
                )
            else:
                raise ValueError(f"unknown action {action!r}")
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            400, "Malformed apply form — go back and run the preview again"
        ) from exc
    updated, skipped_splits = [], 0
    if updates:
        # Re-check split status at write time: a transaction may have become a
        # split between preview render and approval, and patching a split
        # parent's amount would corrupt or be rejected by YNAB.
        ynab = get_ynab()
        current = ynab.get_transactions(
            conversion["budget_id"], conversion["account_id"], conversion["start_date"]
        )
        # Only patch transactions that still exist and are not splits. A txn
        # deleted in YNAB between preview and approval would otherwise make the
        # whole bulk PATCH fail, applying nothing. Also drop anything that got
        # converted or skipped since the preview (stale form, second tab,
        # back-button resubmit) — its marker means that decision already
        # happened, and overwriting would clobber it.
        present_ids = {t["id"] for t in current}
        split_ids = {t["id"] for t in current if is_split(t)}
        stale_ids = {t["id"] for t in current if is_excluded(t)}
        safe = [
            u
            for u in updates
            if u["id"] in present_ids
            and u["id"] not in split_ids
            and u["id"] not in stale_ids
        ]
        skipped_splits = sum(1 for u in updates if u["id"] in split_ids)
        if safe:
            updated = ynab.update_transactions(conversion["budget_id"], safe)
    suffix = f"&skipped_splits={skipped_splits}" if skipped_splits else ""
    return RedirectResponse(
        f"/conversions/{conversion_id}?applied={len(updated)}{suffix}", status_code=303
    )
