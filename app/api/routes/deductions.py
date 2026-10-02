"""Tithe % Deduction endpoints.

Take a percentage off one Sunday's tithe collection - a diocese remittance is the
usual reason - and record it as real Money Out entries, so it reduces the accounts
it came from and shows up in every report. Offerings, Donations, Welfare and the
rest are never touched: only the Tithes category is ever read.

The account chosen in the form does double duty, because that is what the treasurer
is picking in practice: it decides which collection the percentage comes off *and*
where the money leaves from. Choosing ``all`` takes the percentage off the tithes
collected that day across every account and posts one entry per account, so each
balance is reduced by the part that actually came out of it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, TypeGuard, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.core.deps import ClientIp, DbSession
from app.core.permissions import require_permission
from app.db.models import Transaction, User
from app.enums import (
    FUND_DEDUCTION,
    PCT_DEDUCTION_CATEGORY,
    TITHE_SCOPE_ALL,
    TITHE_SCOPE_ALL_LABEL,
    Account,
    AuditAction,
    AuditEntity,
IncomeCategory,
    Permission,
    TitheScope,
    TransactionType,
)
from app.schemas.transaction import (
    DeductionShare,
    PctDeductionCreate,
    PctDeductionPreview,
    PctDeductionResult,
    TithesOnDate,
    TransactionRead,
)
from app.services import accounting, audit
from app.services.realtime import change_event, manager

router = APIRouter(tags=["deductions"])

#: Splitting income across funds rewrites the ledger, so it belongs to the money
#: roles rather than to whoever can read it.
DeductionUser = Annotated[User, Depends(require_permission(Permission.DEDUCTIONS_MANAGE))]

#: Stored as the party on a deduction entry that has no purpose typed in.
DEFAULT_DEDUCTION_PARTY = "Percentage Deduction"


async def _tithes_by_account(db: DbSession, day: date) -> dict[Account, Decimal]:
    """Tithes collected on ``day``, per account, leaving out accounts holding none.

    Internal transfers need no special handling: a transfer row is filed under the
    Transfer category, never under Tithes, so the category filter excludes it.
    """
    rows = (
        (
            await db.execute(
                select(Transaction.account, Transaction.amount)
                .where(
                    Transaction.type == TransactionType.INCOME,
                    Transaction.category == IncomeCategory.TITHES,
                    Transaction.date == day,
                )
                .order_by(Transaction.created_at)
            )
        )
        .all()
    )
    totals: dict[Account, Decimal] = {}
    for account, amount in rows:
        totals[account] = totals.get(account, Decimal("0.00")) + amount
    return {account: total for account, total in totals.items() if total > 0}


async def _tithes(db: DbSession, day: date) -> TithesOnDate:
    """Tithes collected on ``day`` and how many entries made them up."""
    entries = (
        await db.execute(
            select(Transaction.id).where(
                Transaction.type == TransactionType.INCOME,
                Transaction.category == IncomeCategory.TITHES,
                Transaction.date == day,
            )
        )
    ).scalars().all()
    totals = await _tithes_by_account(db, day)
    return TithesOnDate(
        date=day,
        total=accounting.sum_money(totals.values()),
        entry_count=len(entries),
    )


def _is_all(account: TitheScope) -> TypeGuard[Literal["all"]]:
    """Whether the scope is the "all accounts" row rather than a single account.

    Written as a guard so the other branch is narrowed to ``Account`` rather than
    still carrying the string literal.
    """
    return account == TITHE_SCOPE_ALL


def _in_scope(tithes: dict[Account, Decimal], account: TitheScope) -> dict[Account, Decimal]:
    """Narrow per-account tithes to the chosen account, or keep them all."""
    if _is_all(account):
        return tithes
    # Safe: the only other member of TitheScope is Account.
    chosen = cast(Account, account)
    return {chosen: tithes[chosen]} if chosen in tithes else {}


def _nothing_to_deduct(day: date, account: TitheScope) -> str:
    where = TITHE_SCOPE_ALL_LABEL if _is_all(account) else str(account)
    return (
        f"No Tithes were collected in {where} on {day.isoformat()}, "
        "so there is nothing to deduct."
    )


@router.get(
    "/accounting/tithes-on-date",
    response_model=TithesOnDate,
    summary="Tithes collected on a given date",
)
async def tithes_on_date(
    db: DbSession,
    _user: DeductionUser,
    day: date = Query(alias="date"),
) -> TithesOnDate:
    """The base a percentage deduction is calculated from."""
    return await _tithes(db, day)


@router.get(
    "/accounting/pct-deduction-preview",
    response_model=PctDeductionPreview,
    summary="Preview what a percentage deduction would come to",
)
async def preview_pct_deduction(
    db: DbSession,
    _user: DeductionUser,
    day: date = Query(alias="date"),
    pct: Decimal = Query(ge=0, le=100),
    account: TitheScope = Account.CASH,
) -> PctDeductionPreview:
    """Live figures for the form, so the amount updates as the user types."""
    totals = _in_scope(await _tithes_by_account(db, day), account)
    total = accounting.sum_money(totals.values())
    shares = accounting.deduction_shares(totals, pct)
    return PctDeductionPreview(
        date=day,
        pct=pct,
        tithes_that_day=total,
        deduction_amount=accounting.deduction_amount(total, pct),
        shares=[
            DeductionShare(
                account=share.account,
                tithes=share.tithes,
                deduction_amount=share.amount,
            )
            for share in shares
        ],
    )


@router.get(
    "/transactions/pct-deductions",
    response_model=list[TransactionRead],
    summary="Percentage deductions recorded so far",
)
async def list_pct_deductions(db: DbSession, _user: DeductionUser) -> list[TransactionRead]:
    """History for the Tithe % Deduction page, newest first."""
    rows = (
        (
            await db.execute(
                select(Transaction)
                .where(Transaction.category == PCT_DEDUCTION_CATEGORY)
                .order_by(Transaction.date.desc(), Transaction.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [TransactionRead.model_validate(row) for row in rows]


@router.post(
    "/transactions/pct-deduction",
    response_model=PctDeductionResult,
    status_code=status.HTTP_201_CREATED,
    summary="Deduct a percentage of a Sunday's tithes",
)
async def create_pct_deduction(
    payload: PctDeductionCreate, db: DbSession, user: DeductionUser, ip: ClientIp
) -> PctDeductionResult:
    """Record the deduction as an expense against the account it came from.

    The amount is always derived from the tithes actually recorded for that date
    in the chosen scope, so a tampered or stale client cannot post a figure that
    does not tie back to the collection.

    One account gives a single expense entry. ``all`` gives one entry per account
    that held tithes, each for its own share, so no balance is charged for money
    collected somewhere else.
    """
    totals = _in_scope(await _tithes_by_account(db, payload.date), payload.account)
    collected = accounting.sum_money(totals.values())
    if collected <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_nothing_to_deduct(payload.date, payload.account),
        )

    shares = accounting.deduction_shares(totals, payload.pct)
    amount = accounting.deduction_amount(collected, payload.pct)
    if not shares or amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="That percentage comes to KSh 0. Enter a larger percentage.",
        )

    split = len(shares) > 1
    transactions = [
        Transaction(
            type=TransactionType.EXPENSE,
            category=PCT_DEDUCTION_CATEGORY,
            party=(payload.notes or DEFAULT_DEDUCTION_PARTY)[:200],
            amount=share.amount,
            fund=FUND_DEDUCTION,
            account=share.account,
            notes=payload.notes,
            date=payload.date,
            pct=payload.pct,
            base_total=share.tithes,
            created_by_id=user.id,
        )
        for share in shares
    ]
    db.add_all(transactions)
    await audit.record(
        db,
        action=AuditAction.CREATE,
        entity=AuditEntity.TRANSACTION,
        entity_id=transactions[0].id,
        summary=(
            f"{user.full_name} deducted {payload.pct}% ({amount}) of the "
            f"{collected} Tithes collected on {payload.date.isoformat()} "
            f"from {payload.account}"
        ),
        actor=user,
        changes={
            "amount": amount,
            "pct": payload.pct,
            "base_total": collected,
            "account": payload.account,
            "split_between_accounts": split,
            "by_account": {str(share.account): str(share.amount) for share in shares},
        },
        ip_address=ip,
    )
    await db.commit()
    for transaction in transactions:
        await db.refresh(transaction)

    created = [TransactionRead.model_validate(transaction) for transaction in transactions]
    await manager.broadcast(
        change_event(
            "transactions",
            "created",
            [item.model_dump(mode="json") for item in created],
        )
    )
    return PctDeductionResult(
        transactions=created,
        tithes_that_day=collected,
        deduction_amount=amount,
        shares=[
            DeductionShare(
                account=share.account,
                tithes=share.tithes,
                deduction_amount=share.amount,
            )
            for share in shares
        ],
    )