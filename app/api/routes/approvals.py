"""Money-out approval workflow.

A request is raised by whoever spots the expense, sits as ``pending`` while it is
discussed, and only becomes part of the ledger when a money role approves it.

Three properties are worth stating because they are what makes the workflow
trustworthy:

* **A pending request moves nothing.** No balance, report, trial balance or
  dashboard figure reads this table, so a request that is never approved costs
  the ledger nothing. Approving is the only path that writes a transaction.
* **Deciding twice is refused.** ``approve`` and ``reject`` both require
  ``status == pending``, so a double-click or a retried request cannot create two
  expenses. The approval and the transaction it creates are written in a single
  commit.
* **Raising and deciding are separate permissions.** A secretary can notice a bill
  and put it in the queue without being able to authorise the spending; only the
  accountant (or an administrator) can turn a request into a ledger entry.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.deps import ClientIp, DbSession
from app.core.permissions import require_permission
from app.db.models import ApprovalRequest, Transaction, User
from app.enums import ApprovalStatus, AuditAction, AuditEntity, Permission, TransactionType
from app.schemas.approval import (
    ApprovalCreate,
    ApprovalDecision,
    ApprovalRead,
    ApprovalSummary,
)
from app.schemas.transaction import TransactionCreate, TransactionRead
from app.services import audit
from app.services.realtime import change_event, manager

router = APIRouter(prefix="/approvals", tags=["approvals"])

ApprovalsReader = Annotated[User, Depends(require_permission(Permission.APPROVALS_VIEW))]
ApprovalsDecider = Annotated[User, Depends(require_permission(Permission.APPROVALS_DECIDE))]


@router.get("", response_model=list[ApprovalRead], summary="List approval requests")
async def list_approvals(
    db: DbSession,
    _user: ApprovalsReader,
    request_status: ApprovalStatus | None = Query(default=None, alias="status"),
    mine: bool = Query(default=False, description="Only requests raised by the caller."),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[ApprovalRead]:
    statement = select(ApprovalRequest)
    if request_status is not None:
        statement = statement.where(ApprovalRequest.status == request_status)
    if mine:
        statement = statement.where(ApprovalRequest.requested_by_id == _user.id)
    statement = (
        statement.order_by(
            # Pending first so the queue reads top-down, then newest first.
            ApprovalRequest.status.asc(),
            ApprovalRequest.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(statement)).scalars().all()
    return [ApprovalRead.model_validate(row) for row in rows]


@router.get("/count", response_model=ApprovalSummary, summary="Count requests by status")
async def count_approvals(db: DbSession, _user: ApprovalsReader) -> ApprovalSummary:
    rows = (
        await db.execute(
            select(ApprovalRequest.status, func.count()).group_by(ApprovalRequest.status)
        )
    ).all()
    counts = dict.fromkeys(ApprovalStatus, 0)
    counts.update({key: int(count) for key, count in rows})
    return ApprovalSummary(
        pending=counts[ApprovalStatus.PENDING],
        approved=counts[ApprovalStatus.APPROVED],
        rejected=counts[ApprovalStatus.REJECTED],
        total=sum(counts.values()),
    )


@router.post(
    "",
    response_model=ApprovalRead,
    status_code=status.HTTP_201_CREATED,
    summary="Raise a money-out request",
)
async def create_approval(
    payload: ApprovalCreate,
    db: DbSession,
    user: Annotated[User, Depends(require_permission(Permission.APPROVALS_REQUEST))],
    ip: ClientIp,
) -> ApprovalRead:
    request = ApprovalRequest(
        **payload.model_dump(),
        status=ApprovalStatus.PENDING,
        requested_by_id=user.id,
    )
    db.add(request)
    await audit.record(
        db,
        action=AuditAction.CREATE,
        entity=AuditEntity.APPROVAL,
        entity_id=request.id,
        summary=(
            f"{user.full_name} requested {payload.amount} for {payload.category}"
            f"{f' ({payload.party})' if payload.party else ''}"
        ),
        actor=user,
        changes=audit.snapshot(request, ("category", "party", "amount", "fund", "account", "date")),
        ip_address=ip,
    )
    await db.commit()
    await db.refresh(request)

    await manager.broadcast(
        change_event("approvals", "created", entity_id=str(request.id))
    )
    return ApprovalRead.model_validate(request)


async def _get_pending(db: DbSession, approval_id: uuid.UUID) -> ApprovalRequest:
    """Fetch a request that is still awaiting a decision."""
    request = await db.get(ApprovalRequest, approval_id)
    if request is None:
        raise HTTPException(status_code=404, detail="This request no longer exists.")
    if request.status is not ApprovalStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This request was already {request.status.value}.",
        )
    return request


@router.post(
    "/{approval_id}/approve",
    response_model=ApprovalRead,
    summary="Approve a request and record the expense",
)
async def approve_request(
    approval_id: uuid.UUID,
    payload: ApprovalDecision,
    db: DbSession,
    decider: ApprovalsDecider,
    ip: ClientIp,
) -> ApprovalRead:
    """Approve, then write the matching expense into the ledger.

    The transaction is validated through ``TransactionCreate`` first, so a request
    that was raised against rules that have since changed is refused here rather
    than corrupting the ledger.
    """
    request = await _get_pending(db, approval_id)

    transaction_input = TransactionCreate(
        type=TransactionType.EXPENSE,
        category=request.category,
        party=request.party,
        amount=request.amount,
        fund=request.fund,
        account=request.account,
        notes=request.notes,
        date=request.date,
    )
    transaction = Transaction(**transaction_input.model_dump(), created_by_id=decider.id)
    db.add(transaction)
    await db.flush()  # assign the id before linking the request to it

    request.status = ApprovalStatus.APPROVED
    request.decided_by_id = decider.id
    request.decided_at = datetime.now(UTC)
    request.decision_note = payload.note
    request.transaction_id = transaction.id

    await audit.record(
        db,
        action=AuditAction.APPROVE,
        entity=AuditEntity.APPROVAL,
        entity_id=request.id,
        summary=(
            f"{decider.full_name} approved {request.amount} for {request.category}"
            f" and recorded it in the ledger"
        ),
        actor=decider,
        changes={"status": [ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value]},
        ip_address=ip,
    )
    await audit.record(
        db,
        action=AuditAction.CREATE,
        entity=AuditEntity.TRANSACTION,
        entity_id=transaction.id,
        summary=(
            f"{request.amount} {request.category} recorded by {decider.full_name}"
            f" from an approved request"
        ),
        actor=decider,
        changes=audit.snapshot(
            transaction, ("type", "category", "party", "amount", "fund", "account", "date")
        ),
        ip_address=ip,
    )
    await db.commit()
    await db.refresh(request)

    # Two events: the request changed state, and the ledger gained a row.
    await manager.broadcast(change_event("approvals", "updated", entity_id=str(request.id)))
    await manager.broadcast(
        change_event(
            "transactions",
            "created",
            TransactionRead.model_validate(transaction).model_dump(mode="json"),
        )
    )
    return ApprovalRead.model_validate(request)


@router.post(
    "/{approval_id}/reject",
    response_model=ApprovalRead,
    summary="Reject a request",
)
async def reject_request(
    approval_id: uuid.UUID,
    payload: ApprovalDecision,
    db: DbSession,
    decider: ApprovalsDecider,
    ip: ClientIp,
) -> ApprovalRead:
    """Reject without touching the ledger."""
    request = await _get_pending(db, approval_id)

    request.status = ApprovalStatus.REJECTED
    request.decided_by_id = decider.id
    request.decided_at = datetime.now(UTC)
    request.decision_note = payload.note

    await audit.record(
        db,
        action=AuditAction.REJECT,
        entity=AuditEntity.APPROVAL,
        entity_id=request.id,
        summary=f"{decider.full_name} rejected the {request.amount} {request.category} request",
        actor=decider,
        changes={"status": [ApprovalStatus.PENDING.value, ApprovalStatus.REJECTED.value]},
        ip_address=ip,
    )
    await db.commit()
    await db.refresh(request)

    await manager.broadcast(change_event("approvals", "updated", entity_id=str(request.id)))
    return ApprovalRead.model_validate(request)