"""Audit trail endpoints.

Reading the trail is restricted to administrators. The ledger itself is open to
every signed-in user, but this log carries IP addresses, role changes and failed
sign-in attempts, which is governance information rather than church data.

There is deliberately no write, update or delete endpoint here. Entries are only
ever produced by :mod:`app.services.audit` from inside the handlers that made the
change, so the trail cannot be edited through the API even by an administrator.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

from fastapi import APIRouter, Query
from sqlalchemy import func, or_, select

from app.core.deps import DbSession, Superuser
from app.db.models import AuditEntry
from app.enums import AuditAction, AuditEntity
from app.schemas.audit import AuditEntryRead
from app.schemas.common import Page

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get(
    "",
    response_model=Page[AuditEntryRead],
    summary="Read the audit trail, newest first",
)
async def list_audit_entries(
    db: DbSession,
    _admin: Superuser,
    action: AuditAction | None = None,
    entity: AuditEntity | None = None,
    actor_id: uuid.UUID | None = Query(default=None, description="Filter to one user."),
    search: str | None = Query(default=None, max_length=200),
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Page[AuditEntryRead]:
    """Return one page of entries plus the total, so the screen can paginate.

    The total is counted with the same filters as the page: without it the client
    would have to download the whole trail to discover whether a next page exists.
    """
    filters = []
    if action is not None:
        filters.append(AuditEntry.action == action)
    if entity is not None:
        filters.append(AuditEntry.entity_type == entity)
    if actor_id is not None:
        filters.append(AuditEntry.actor_id == actor_id)
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(AuditEntry.summary.ilike(pattern), AuditEntry.actor_email.ilike(pattern))
        )
    if date_from is not None:
        filters.append(AuditEntry.created_at >= datetime.combine(date_from, time.min))
    if date_to is not None:
        # Inclusive of the whole end day: the client sends a calendar date, not a
        # timestamp, and a range ending "2026-10-02" should include that day.
        filters.append(AuditEntry.created_at <= datetime.combine(date_to, time.max))

    total = int(
        await db.scalar(select(func.count()).select_from(AuditEntry).where(*filters)) or 0
    )

    statement = (
        select(AuditEntry)
        .where(*filters)
        .order_by(AuditEntry.created_at.desc(), AuditEntry.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = (await db.execute(statement)).scalars().all()

    return Page[AuditEntryRead](
        items=[AuditEntryRead.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size,
    )