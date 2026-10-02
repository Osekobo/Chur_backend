"""Tithe % Deduction endpoints.

Take a percentage off a single Sunday's tithe collection - a diocese
remittance is the usual reason - and record it as a real Money Out entry, so it
reduces the account it was posted to and shows up in every report. Only Tithes
are affected; Offerings, Donations, Welfare and the rest are never touched.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.core.deps import ClientIp, CurrentUser, DbSession
from app.db.models import Transaction
from app.enums import (
    FUND_DEDUCTION,
    PCT_DEDUCTION_CATEGORY,
    AuditAction,
    AuditEntity,
    IncomeCategory,
    TransactionType,
)
from app.schemas.transaction import (
    PctDeductionCreate,
    PctDeductionPreview,
    PctDeductionResult,
    TithesOnDate,
    TransactionRead,
)
from app.services import accounting, audit
from app.services.realtime import change_event, manager

router = APIRouter(tags=["deductions"])

#: Stored as the party on a deduction entry that has no purpose typed in.
DEFAULT_DEDUCTION_PARTY = "Percentage Deduction"


async def _tithes(db: DbSession, day: date) -> TithesOnDate:
    """Tithes collected on ``day`` and how many entries made them up."""
    rows = (
        (
            await db.execute(
                select(Transaction.amount)
                .where(
                    Transaction.type == TransactionType.INCOME,
                    Transaction.category == IncomeCategory.TITHES,
                    Transaction.date == day,
                )
                .order_by(Transaction.created_at)
            )
        )
        .scalars()
        .all()
    )
    return TithesOnDate(
        date=day,
        total=accounting.sum_money(rows),
        entry_count=len(rows),
    )


@router.get(
    "/accounting/tithes-on-date",
    response_model=TithesOnDate,
    summary="Tithes collected on a given date",
)
async def tithes_on_date(
    db: DbSession,
    _user: CurrentUser,
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
    _user: CurrentUser,
    day: date = Query(alias="date"),
    pct: Decimal = Query(ge=0, le=100),
) -> PctDeductionPreview:
    """Live figures for the form, so the amount updates as the user types."""
    total = (await _tithes(db, day)).total
    return PctDeductionPreview(
        date=day,
        pct=pct,
        tithes_that_day=total,
        deduction_amount=accounting.deduction_amount(total, pct),
    )


@router.get(
    "/transactions/pct-deductions",
    response_model=list[TransactionRead],
    summary="Percentage deductions recorded so far",
)
async def list_pct_deductions(db: DbSession, _user: CurrentUser) -> list[TransactionRead]:
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
    payload: PctDeductionCreate, db: DbSession, user: CurrentUser, ip: ClientIp
) -> PctDeductionResult:
    """Record the deduction as an expense against the chosen account.

    The amount is always derived from the tithes actually recorded for that
    date, so a tampered or stale client cannot post a figure that does not tie
    back to the collection.
    """
    tithes = await _tithes(db, payload.date)
    if tithes.total <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"No Tithes were recorded for {payload.date.isoformat()}, "
                "so there is nothing to deduct."
            ),
        )

    amount = accounting.deduction_amount(tithes.total, payload.pct)
    if amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="That percentage comes to KSh 0. Enter a larger percentage.",
        )

    transaction = Transaction(
        type=TransactionType.EXPENSE,
        category=PCT_DEDUCTION_CATEGORY,
        party=(payload.notes or DEFAULT_DEDUCTION_PARTY)[:200],
        amount=amount,
        fund=FUND_DEDUCTION,
        account=payload.account,
        notes=payload.notes,
        date=payload.date,
        pct=payload.pct,
        base_total=tithes.total,
        created_by_id=user.id,
    )
    db.add(transaction)
    await audit.record(
        db,
        action=AuditAction.CREATE,
        entity=AuditEntity.TRANSACTION,
        entity_id=transaction.id,
        summary=(
            f"{user.full_name} deducted {payload.pct}% ({amount}) of the "
            f"{tithes.total} tithes collected on {payload.date.isoformat()}"
        ),
        actor=user,
        changes={
            "amount": amount,
            "pct": payload.pct,
            "base_total": tithes.total,
            "account": payload.account,
        },
        ip_address=ip,
    )
    await db.commit()
    await db.refresh(transaction)

    result = TransactionRead.model_validate(transaction)
    await manager.broadcast(change_event("transactions", "created", result.model_dump(mode="json")))
    return PctDeductionResult(
        transaction=result,
        tithes_that_day=tithes.total,
        deduction_amount=amount,
    )
