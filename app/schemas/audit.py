"""Audit trail schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.enums import AuditAction, AuditEntity
from app.schemas.common import ORMModel


class AuditEntryRead(ORMModel):
    """One line of the trail, as shown on the Audit screen.

    ``changes`` is a free-form map rather than a typed object on purpose: the
    fields differ per entity (a transaction edit records money, a role change
    records a flag) and one schema has to cover all of them.
    """

    id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_email: str
    action: AuditAction
    entity_type: AuditEntity
    entity_id: str | None
    summary: str
    changes: dict[str, Any] | None
    ip_address: str
    created_at: datetime
