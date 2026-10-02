"""Shared FastAPI dependencies."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import TokenError, decode_access_token, hash_token
from app.db.models import RefreshToken, User
from app.db.session import get_db

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_client_ip(request: Request) -> str:
    """Best-effort caller address for the audit trail.

    ``x-forwarded-for`` wins when present so the log records the real client
    rather than the reverse proxy. Only the first entry is kept. Not a security
    control - it exists to answer "where did this come from".
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


ClientIp = Annotated[str, Depends(get_client_ip)]

CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials.",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    db: DbSession, token: Annotated[str | None, Depends(oauth2_scheme)]
) -> User:
    """Resolve the authenticated user from the ``Authorization: Bearer`` header."""
    if not token:
        raise CREDENTIALS_ERROR

    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(payload["sub"])
    except (TokenError, ValueError, KeyError) as exc:
        raise CREDENTIALS_ERROR from exc

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise CREDENTIALS_ERROR
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_active_superuser(user: CurrentUser) -> User:
    if not user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The user does not have enough privileges.",
        )
    return user


#: Gate for the administrator-only endpoints.
Superuser = Annotated[User, Depends(get_current_active_superuser)]


async def revoke_refresh_token(db: AsyncSession, raw_token: str) -> None:
    """Mark a refresh token as revoked. Unknown tokens are ignored."""
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(raw_token))
        .values(revoked_at=datetime.now(UTC))
    )
    await db.commit()


async def get_user_for_refresh_token(db: AsyncSession, raw_token: str) -> User | None:
    """Look up the user owning a valid (non-expired, non-revoked) refresh token."""
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token))
    )
    record = result.scalar_one_or_none()
    if record is None or record.revoked_at is not None or record.expires_at <= datetime.now(UTC):
        return None
    return await db.get(User, record.user_id)
