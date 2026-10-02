"""Administrator-only user management.

Every endpoint here is gated on :data:`Permission.USERS_MANAGE`, so a signed-in
accountant or secretary gets 403 regardless of what they ask for.

Three deliberate choices:

* **There is no hard delete.** Accounts are deactivated (``is_active = false``)
  instead. ``transactions.created_by_id`` points at this table, and the ledger
  is the audit record; removing the row would blank out who recorded an entry.
  Deactivation immediately blocks the account: ``get_current_user`` re-checks
  ``is_active`` on every request, so an already-issued access token dies at once.
* **An administrator cannot demote or deactivate themselves.** This single rule
  is what makes permanent lockout impossible, and it needs no counting argument:
  reaching any endpoint here already proves the caller is an active administrator
  (``Permission.USERS_MANAGE`` is held only by that role, and the dependency chains
  ``get_current_user``, which rejects inactive accounts). So the caller always
  remains one administrator, and any *other* account can safely be demoted or
  deactivated. Since ``scripts/seed.py`` is gone there is no CLI back door, so
  this refusal is the only thing standing between an operator and a system
  nobody can administer.
* **Passwords are hashed with the same Argon2id helper as everywhere else**, so
  an administrator-set password is indistinguishable from a self-chosen one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select, update

from app.core.deps import ClientIp, DbSession
from app.core.permissions import require_permission
from app.core.security import hash_password
from app.db.models import RefreshToken, User
from app.enums import AuditAction, AuditEntity, Permission, UserRole
from app.schemas.auth import (
    AdminUserCreate,
    SetPasswordRequest,
    UserRead,
    UserUpdate,
)
from app.schemas.common import Message
from app.services import audit

router = APIRouter(prefix="/users", tags=["users"])

AccountsAdmin = Annotated[User, Depends(require_permission(Permission.USERS_MANAGE))]


def _normalise_email(email: str) -> str:
    return email.strip().lower()


@router.get("", response_model=list[UserRead], summary="List user accounts")
async def list_users(db: DbSession, _admin: AccountsAdmin) -> list[UserRead]:
    rows = (
        (await db.execute(select(User).order_by(User.full_name, User.email)))
        .scalars()
        .all()
    )
    return [UserRead.model_validate(row) for row in rows]


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user account",
)
async def create_user(
    payload: AdminUserCreate, db: DbSession, admin: AccountsAdmin, ip: ClientIp
) -> UserRead:
    """Provision an account with its role already assigned.

    Unlike ``POST /auth/register`` this does not sign the new account in - the
    administrator chooses the role, so the account is not usable until it is
    handed over.
    """
    email = _normalise_email(payload.email)
    if await db.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email address already exists.",
        )

    user = User(
        email=email,
        full_name=payload.full_name.strip(),
        hashed_password=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    await audit.record(
        db,
        action=AuditAction.CREATE,
        entity=AuditEntity.USER,
        entity_id=user.id,
        summary=f"{admin.full_name} created the {user.role.label} account {user.email}",
        actor=admin,
        changes={
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role.value,
        },
        ip_address=ip,
    )
    await db.commit()
    await db.refresh(user)
    return UserRead.model_validate(user)


@router.patch("/{user_id}", response_model=UserRead, summary="Change a role or status")
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    db: DbSession,
    admin: AccountsAdmin,
    ip: ClientIp,
) -> UserRead:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="This account no longer exists.")

    # Read the affected fields *before* mutating, so the log records old -> new.
    before = audit.snapshot(user, ("full_name", "email", "role", "is_active"))
    requested = payload.model_dump(exclude_unset=True)

    # Reaching this point means the caller is an active administrator (see the
    # module docstring), so refusing to strip the caller's own access is enough to
    # guarantee at least one administrator always remains.
    if user.id == admin.id and (
        ("role" in requested and requested["role"] != UserRole.ADMIN)
        or requested.get("is_active") is False
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "You cannot remove your own administrator access. "
                "Ask another administrator to do it."
            ),
        )

    for field, value in requested.items():
        setattr(user, field, value)

    # A deactivated account must not be able to mint a new access token from a
    # refresh token that is still sitting in the database.
    if requested.get("is_active") is False:
        await db.execute(
            delete(RefreshToken).where(
                RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
            )
        )

    after = audit.snapshot(user, ("full_name", "email", "role", "is_active"))
    changed = audit.diff(before, after)
    if changed:
        # A role flip and a deactivation are different events with different
        # weight, so they get their own action rather than a generic "update".
        if "role" in changed:
            action = AuditAction.ROLE_CHANGE
        elif changed.get("is_active") == [True, False]:
            action = AuditAction.ACCOUNT_DEACTIVATED
        elif changed.get("is_active") == [False, True]:
            action = AuditAction.ACCOUNT_REACTIVATED
        else:
            action = AuditAction.UPDATE
        await audit.record(
            db,
            action=action,
            entity=AuditEntity.USER,
            entity_id=user.id,
            summary=(
                f"{admin.full_name} updated the account {user.email}"
                + (f" to {user.role.label}" if "role" in changed else "")
            ),
            actor=admin,
            changes=changed,
            ip_address=ip,
        )

    await db.commit()
    await db.refresh(user)
    return UserRead.model_validate(user)


@router.post(
    "/{user_id}/password",
    response_model=Message,
    summary="Set a user's password",
)
async def set_password(
    user_id: uuid.UUID,
    payload: SetPasswordRequest,
    db: DbSession,
    admin: AccountsAdmin,
    ip: ClientIp,
) -> Message:
    """Overwrite a password without knowing the old one.

    Every outstanding session for that account is revoked, matching what
    ``POST /auth/change-password`` does for a self-service change.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="This account no longer exists.")

    user.hashed_password = hash_password(payload.new_password)
    await db.execute(
        delete(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
        )
    )
    await audit.record(
        db,
        action=AuditAction.PASSWORD_RESET,
        entity=AuditEntity.USER,
        entity_id=user.id,
        summary=f"{admin.full_name} reset the password for {user.email}",
        actor=admin,
        ip_address=ip,
    )
    await db.commit()
    return Message(message=f"Password updated for {user.email}.")


@router.post(
    "/{user_id}/sessions",
    response_model=Message,
    summary="Sign a user out everywhere",
)
async def revoke_sessions(
    user_id: uuid.UUID,
    db: DbSession,
    admin: AccountsAdmin,
    ip: ClientIp,
) -> Message:
    """Force a sign-out by revoking every refresh token for the account."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="This account no longer exists.")

    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    await audit.record(
        db,
        action=AuditAction.LOGOUT,
        entity=AuditEntity.USER,
        entity_id=user.id,
        summary=f"{admin.full_name} signed {user.email} out of all sessions",
        actor=admin,
        ip_address=ip,
    )
    await db.commit()
    return Message(message=f"{user.email} signed out of all sessions.")