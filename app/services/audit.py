"""Writes to the append-only audit trail.

Called explicitly from the route handlers rather than from middleware: only the
handler knows *who* acted, *which* record was touched and what the before/after
values were, and a middleware would have to guess all three from the URL.

Every function here adds the row to the caller's session and leaves the commit to
the caller, so the audit entry and the change it describes are written in one
transaction. If the caller's work rolls back, so does its audit entry - which is
what keeps the log honest.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditEntry, User
from app.enums import AuditAction, AuditEntity


def _jsonable(value: Any) -> Any:
    """Coerce a value into something JSONB accepts.

    MONEY arrives as ``Decimal`` and datetimes as ``datetime``; both would raise
    from the driver, so they are turned into strings here.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (uuid.UUID,)):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def snapshot(obj: object, fields: tuple[str, ...]) -> dict[str, Any]:
    """Read the given attributes off an ORM row, JSON-safe.

    Call this *before* mutating the row so the log keeps the old value.
    """
    return {name: _jsonable(getattr(obj, name, None)) for name in fields}


async def record(
    db: AsyncSession,
    *,
    action: AuditAction,
    entity: AuditEntity,
    summary: str,
    actor: User | None = None,
    actor_email: str = "",
    entity_id: uuid.UUID | str | None = None,
    changes: dict[str, Any] | None = None,
    ip_address: str = "",
) -> None:
    """Queue one audit entry.

    Args:
        db: the caller's session; the entry is flushed with their next commit.
        actor: the signed-in user, or ``None`` for anonymous events such as a
            failed sign-in, in which case pass ``actor_email`` to record who was
            being targeted.
        summary: one readable line, e.g. "Deleted transaction 12.00 Expenses".
    """
    db.add(
        AuditEntry(
            actor_id=actor.id if actor is not None else None,
            actor_email=actor.email if actor is not None else actor_email,
            action=action,
            entity_type=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            summary=summary[:500],
            changes={key: _jsonable(value) for key, value in changes.items()}
            if changes
            else None,
            ip_address=ip_address[:45],
        )
    )


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Keep only the keys that actually changed, as ``{field: [old, new]}``.

    An edit that re-saves the same values should not produce a log line that
    claims something changed.
    """
    result: dict[str, Any] = {}
    for key, new_value in after.items():
        old_value = before.get(key)
        if old_value != new_value:
            result[key] = [old_value, new_value]
    return result