"""Transaction, transfer and account/fund endpoints."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import ColumnElement, case, delete, func, or_, select

from app.core.config import settings
from app.core.deps import ClientIp, CurrentUser, DbSession
from app.core.permissions import ensure_permission, require_permission
from app.db.models import Person, Transaction, User
from app.enums import (
    ACCOUNTS,
    FUNDS,
    TRANSFER_CATEGORY,
    Account,
    AuditAction,
    AuditEntity,
    Fund,
    Permission,
    TransactionType,
)
from app.schemas.common import Message
from app.schemas.transaction import (
    FundSummary,
    NamedTotal,
    TransactionCreate,
    TransactionRead,
    TransferCreate,
    TransferRead,
)
from app.services import accounting, audit
from app.services.realtime import change_event, manager

router = APIRouter(prefix="/transactions", tags=["transactions"])


# --------------------------------------------------------------------------- #
# Aggregate helpers (computed in SQL so the client never downloads the ledger
# just to render a balance card).
# --------------------------------------------------------------------------- #
def _net_balance_expression() -> ColumnElement[int]:
    signed_amount = case(
        (Transaction.type == TransactionType.INCOME, Transaction.amount),
        else_=-Transaction.amount,
    )
    return func.coalesce(func.sum(signed_amount), 0)


#: Aggregates come back from Postgres as ``NUMERIC``, but a group with no matching
#: rows yields the integer ``0`` from COALESCE. Quantize so every money figure in a
#: response serialises with the same two decimals instead of mixing "0" and "0.00".
_CENTS = Decimal("0.01")


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENTS)


@router.get(
    "/accounts/balances", response_model=list[NamedTotal], summary="Balance per payment account"
)
async def account_balances(db: DbSession, _user: CurrentUser) -> list[NamedTotal]:
    rows = (
        await db.execute(
            select(Transaction.account, _net_balance_expression())
            .where(Transaction.account.in_(ACCOUNTS))
            .group_by(Transaction.account)
        )
    ).all()
    balances = {str(account): Decimal(str(total or 0)) for account, total in rows}
    return [
        NamedTotal(key=account, total=balances.get(account, Decimal("0.00")))
        for account in ACCOUNTS
    ]


@router.get("/funds/net", response_model=list[NamedTotal], summary="Net movement per fund")
async def fund_balances(db: DbSession, _user: CurrentUser) -> list[NamedTotal]:
    rows = (
        await db.execute(
            select(Transaction.fund, _net_balance_expression())
            .where(Transaction.fund.in_(FUNDS))
            .group_by(Transaction.fund)
        )
    ).all()
    balances = {str(fund): Decimal(str(total or 0)) for fund, total in rows}
    return [NamedTotal(key=fund, total=balances.get(fund, Decimal("0.00"))) for fund in FUNDS]


@router.get(
    "/funds/summary", response_model=list[FundSummary], summary="Received and spent per fund"
)
async def fund_summary(db: DbSession, _user: CurrentUser) -> list[FundSummary]:
    """Return received, spent and net for every fund.

    Aggregated in SQL rather than by the client summing a capped page of rows,
    which would under-report once a fund passes the page size.
    """
    received = func.coalesce(
        func.sum(case((Transaction.type == TransactionType.INCOME, Transaction.amount), else_=0)),
        0,
    )
    spent = func.coalesce(
        func.sum(case((Transaction.type == TransactionType.EXPENSE, Transaction.amount), else_=0)),
        0,
    )
    rows = (
        await db.execute(
            select(Transaction.fund, received, spent)
            .where(Transaction.fund.in_(FUNDS))
            .group_by(Transaction.fund)
        )
    ).all()
    by_fund = {
        str(fund): (_money(in_amt), _money(out_amt)) for fund, in_amt, out_amt in rows
    }
    zero = Decimal("0.00")
    return [
        FundSummary(
            fund=fund,
            received=by_fund.get(fund, (zero, zero))[0],
            spent=by_fund.get(fund, (zero, zero))[1],
            net=by_fund.get(fund, (zero, zero))[0] - by_fund.get(fund, (zero, zero))[1],
        )
        for fund in FUNDS
    ]


@router.get("/transfers", response_model=list[TransferRead], summary="List paired transfers")
async def list_transfers(db: DbSession, _user: CurrentUser) -> list[TransferRead]:
    """Return one row per transfer, pairing the outgoing and incoming legs."""
    rows = (
        (
            await db.execute(
                select(Transaction)
                .where(Transaction.category == TRANSFER_CATEGORY)
                .order_by(Transaction.date.desc(), Transaction.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        TransferRead(
            transfer_id=pair.transfer_id,
            date=pair.date,
            from_account=pair.source_account,
            to_account=pair.destination_account,
            amount=pair.amount,
            notes=pair.notes,
        )
        for pair in accounting.pair_transfers(rows)
    ]


@router.post(
    "/transfers",
    response_model=TransferRead,
    status_code=status.HTTP_201_CREATED,
    summary="Move money between accounts",
)
async def create_transfer(
    payload: TransferCreate,
    db: DbSession,
    user: Annotated[User, Depends(require_permission(Permission.TRANSFERS_MANAGE))],
    ip: ClientIp,
) -> TransferRead:
    """Create both legs of a transfer atomically."""
    transfer_id = uuid.uuid4()
    common = {
        "category": TRANSFER_CATEGORY,
        "party": "",
        "amount": payload.amount,
        "fund": "",
        "notes": payload.notes,
        "date": payload.date,
        "transfer_id": transfer_id,
        "created_by_id": user.id,
    }
    db.add_all(
        [
            Transaction(type=TransactionType.EXPENSE, account=payload.from_account, **common),
            Transaction(type=TransactionType.INCOME, account=payload.to_account, **common),
        ]
    )
    await audit.record(
        db,
        action=AuditAction.CREATE,
        entity=AuditEntity.TRANSACTION,
        entity_id=transfer_id,
        summary=(
            f"{user.full_name} moved {payload.amount} from {payload.from_account}"
            f" to {payload.to_account}"
        ),
        actor=user,
        changes={"amount": payload.amount, "date": payload.date},
        ip_address=ip,
    )
    await db.commit()

    await manager.broadcast(change_event("transactions", "created", entity_id=str(transfer_id)))
    return TransferRead(
        transfer_id=transfer_id,
        date=payload.date,
        from_account=payload.from_account,
        to_account=payload.to_account,
        amount=payload.amount,
        notes=payload.notes,
    )


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
@router.get("", response_model=list[TransactionRead], summary="List transactions")
async def list_transactions(
    db: DbSession,
    _user: CurrentUser,
    tx_type: TransactionType | None = Query(default=None, alias="type"),
    category: str | None = Query(default=None, max_length=60),
    account: Account | None = None,
    fund: Fund | None = None,
    party: str | None = Query(default=None, max_length=200),
    search: str | None = Query(default=None, max_length=200),
    date_from: date | None = None,
    date_to: date | None = None,
    include_transfers: bool = True,
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=None, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
) -> list[TransactionRead]:
    statement = select(Transaction)
    if tx_type is not None:
        statement = statement.where(Transaction.type == tx_type)
    if category:
        statement = statement.where(Transaction.category == category)
    if account is not None:
        statement = statement.where(Transaction.account == account)
    if fund is not None:
        statement = statement.where(Transaction.fund == fund)
    if party:
        statement = statement.where(Transaction.party.ilike(f"%{party.strip()}%"))
    if search:
        pattern = f"%{search.strip()}%"
        statement = statement.where(
            or_(Transaction.party.ilike(pattern), Transaction.notes.ilike(pattern))
        )
    if date_from:
        statement = statement.where(Transaction.date >= date_from)
    if date_to:
        statement = statement.where(Transaction.date <= date_to)
    if not include_transfers:
        statement = statement.where(Transaction.category != TRANSFER_CATEGORY)

    statement = statement.order_by(
        Transaction.date.asc() if order == "asc" else Transaction.date.desc(),
        Transaction.created_at.asc(),
    )
    statement = statement.limit(limit or settings.MAX_PAGE_SIZE).offset(offset)

    rows = (await db.execute(statement)).scalars().all()
    return [TransactionRead.model_validate(row) for row in rows]


@router.post(
    "",
    response_model=TransactionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record an income or expense entry",
)
async def create_transaction(
    payload: TransactionCreate, db: DbSession, user: CurrentUser, ip: ClientIp
) -> TransactionRead:
    """Record one entry, in whichever direction the role is allowed to work.

    A single endpoint serves both halves of the ledger, so the check has to wait
    for the payload: a secretary may raise income but not spending. Refusing here,
    after the direction is known, is the only way to express that without two
    near-duplicate endpoints that would drift apart.
    """
    ensure_permission(
        user,
        Permission.MONEY_IN
        if payload.type is TransactionType.INCOME
        else Permission.MONEY_OUT,
    )
    # The giver's name is copied from the directory row rather than accepted from
    # the client, so the two cannot disagree - and so a name in the ledger is
    # always a name that existed in the directory when it was written.
    values = payload.model_dump()
    if payload.type is TransactionType.INCOME:
        person = await db.get(Person, payload.person_id)
        if person is None:
            raise HTTPException(
                status_code=404,
                detail="That person is not in the directory any more. Pick another.",
            )
        values["party"] = person.name
    else:
        values["party"] = payload.party

    transaction = Transaction(**values, created_by_id=user.id)
    db.add(transaction)
    verb = "recorded" if payload.type is TransactionType.INCOME else "spent"
    await audit.record(
        db,
        action=AuditAction.CREATE,
        entity=AuditEntity.TRANSACTION,
        entity_id=transaction.id,
        summary=(
            f"{user.full_name} {verb} {payload.amount} for {payload.category}"
            f"{f' ({transaction.party})' if transaction.party else ''}"
        ),
        actor=user,
        changes=audit.snapshot(
            transaction, ("type", "category", "party", "amount", "fund", "account", "date")
        ),
        ip_address=ip,
    )
    await db.commit()
    await db.refresh(transaction)

    result = TransactionRead.model_validate(transaction)
    await manager.broadcast(change_event("transactions", "created", result.model_dump(mode="json")))
    return result


@router.delete("/{transaction_id}", response_model=Message, summary="Delete a transaction")
async def delete_transaction(
    transaction_id: uuid.UUID, db: DbSession, user: CurrentUser, ip: ClientIp
) -> Message:
    transaction = await db.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(status_code=404, detail="This entry no longer exists.")

    # Deleting rewrites a balance, so it needs the same permission as recording
    # that direction in the first place - measured on the row being removed, not
    # on the caller's intent. A transfer also takes the stronger permission: both
    # of its legs are about moving money that is already banked.
    is_transfer = transaction.transfer_id is not None
    required = (
        Permission.TRANSFERS_MANAGE
        if is_transfer
        else Permission.MONEY_IN
        if transaction.type is TransactionType.INCOME
        else Permission.MONEY_OUT
    )
    ensure_permission(user, required)

    transfer_id = transaction.transfer_id
    # Captured before the row goes away - the log has to describe what was removed.
    detail = audit.snapshot(
        transaction, ("type", "category", "party", "amount", "fund", "account", "date")
    )
    summary = (
        f"{user.full_name} deleted the {transaction.amount} {transaction.category} entry"
        + (" and its matching transfer leg" if transfer_id is not None else "")
    )
    await db.delete(transaction)

    # Deleting one side of a transfer must delete the other side too.
    if transfer_id is not None:
        await db.execute(delete(Transaction).where(Transaction.transfer_id == transfer_id))
    await audit.record(
        db,
        action=AuditAction.DELETE,
        entity=AuditEntity.TRANSACTION,
        entity_id=transfer_id or transaction_id,
        summary=summary,
        actor=user,
        changes=detail,
        ip_address=ip,
    )
    await db.commit()

    await manager.broadcast(
        change_event("transactions", "deleted", entity_id=str(transfer_id or transaction_id))
    )
    return Message(message="Entry deleted.")
