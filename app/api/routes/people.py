"""People directory endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.core.deps import ClientIp, CurrentUser, DbSession
from app.db.models import Person
from app.enums import AuditAction, AuditEntity, PersonRole
from app.schemas.common import Message
from app.schemas.person import PersonCreate, PersonRead, PersonUpdate
from app.services import audit

router = APIRouter(prefix="/people", tags=["people"])


@router.get("", response_model=list[PersonRead], summary="List people")
async def list_people(
    db: DbSession,
    _user: CurrentUser,
    role: PersonRole | None = Query(default=None, description="Filter by directory role."),
    search: str | None = Query(default=None, max_length=200, description="Name or phone contains."),
    limit: int = Query(default=500, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[PersonRead]:
    statement = select(Person)
    if role is not None:
        statement = statement.where(Person.role == role)
    if search:
        pattern = f"%{search.strip().lower()}%"
        statement = statement.where(
            or_(
                func.lower(Person.name).like(pattern),
                func.lower(Person.phone).like(pattern),
            )
        )
    statement = statement.order_by(Person.name).limit(limit).offset(offset)
    rows = (await db.execute(statement)).scalars().all()
    return [PersonRead.model_validate(row) for row in rows]


@router.get("/count", response_model=dict[str, int], summary="Count people per role")
async def count_people(db: DbSession, _user: CurrentUser) -> dict[str, int]:
    rows = (await db.execute(select(Person.role, func.count()).group_by(Person.role))).all()
    counts = {role.value: 0 for role in PersonRole}
    counts.update({role_value: int(count) for role_value, count in rows})
    counts["total"] = sum(count for key, count in counts.items() if key != "total")
    return counts


@router.post(
    "", response_model=PersonRead, status_code=status.HTTP_201_CREATED, summary="Add a person"
)
async def create_person(
    payload: PersonCreate, db: DbSession, user: CurrentUser, ip: ClientIp
) -> PersonRead:
    person = Person(**payload.model_dump())
    db.add(person)
    await audit.record(
        db,
        action=AuditAction.CREATE,
        entity=AuditEntity.PERSON,
        entity_id=person.id,
        summary=f"{user.full_name} added {person.name} to the directory",
        actor=user,
        changes=audit.snapshot(person, ("name", "phone", "email", "role")),
        ip_address=ip,
    )
    try:
        await db.commit()
    except IntegrityError as exc:  # pragma: no cover - defensive
        await db.rollback()
        raise HTTPException(status_code=409, detail="Could not save this person.") from exc
    await db.refresh(person)
    return PersonRead.model_validate(person)


@router.patch("/{person_id}", response_model=PersonRead, summary="Update a person")
async def update_person(
    person_id: uuid.UUID,
    payload: PersonUpdate,
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
) -> PersonRead:
    person = await db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="This person no longer exists.")

    before = audit.snapshot(person, ("name", "phone", "email", "role"))
    requested = payload.model_dump(exclude_unset=True)
    for field, value in requested.items():
        setattr(person, field, value)

    changed = audit.diff(before, audit.snapshot(person, ("name", "phone", "email", "role")))
    if changed:
        await audit.record(
            db,
            action=AuditAction.UPDATE,
            entity=AuditEntity.PERSON,
            entity_id=person.id,
            summary=f"{user.full_name} updated {person.name}",
            actor=user,
            changes=changed,
            ip_address=ip,
        )
    await db.commit()
    await db.refresh(person)
    return PersonRead.model_validate(person)


@router.delete("/{person_id}", response_model=Message, summary="Delete a person")
async def delete_person(
    person_id: uuid.UUID, db: DbSession, user: CurrentUser, ip: ClientIp
) -> Message:
    person = await db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="This person no longer exists.")
    name = person.name
    await audit.record(
        db,
        action=AuditAction.DELETE,
        entity=AuditEntity.PERSON,
        entity_id=person.id,
        summary=f"{user.full_name} deleted {name} from the directory",
        actor=user,
        changes=audit.snapshot(person, ("name", "phone", "email", "role")),
        ip_address=ip,
    )
    await db.delete(person)
    await db.commit()
    return Message(message=f"{name} deleted.")
