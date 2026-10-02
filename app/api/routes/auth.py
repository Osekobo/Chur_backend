"""Authentication endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import (
    ClientIp,
    CurrentUser,
    DbSession,
    get_user_for_refresh_token,
    revoke_refresh_token,
)
from app.core.security import (
    create_access_token,
    generate_opaque_token,
    hash_password,
    hash_token,
    password_reset_expiry,
    refresh_token_expiry,
    verify_password,
)
from app.db.models import PasswordResetToken, RefreshToken, User
from app.enums import AuditAction, AuditEntity
from app.schemas.auth import (
    AuthSession,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginResponse,
    RefreshRequest,
    ResetPasswordRequest,
    UserCreate,
    UserLogin,
    UserRead,
)
from app.schemas.common import Message, TokenPair
from app.services import audit
from app.services.notifications import build_reset_url, send_password_reset_email

router = APIRouter(prefix="/auth", tags=["auth"])


async def _issue_tokens(db: AsyncSession, user: User) -> TokenPair:
    """Create an access token plus a persisted, revocable refresh token."""
    raw_refresh_token = generate_opaque_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(raw_refresh_token),
            expires_at=refresh_token_expiry(),
        )
    )
    await db.commit()
    return TokenPair(
        access_token=create_access_token(user.id),
        refresh_token=raw_refresh_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


def _normalise_email(email: str) -> str:
    return email.strip().lower()


@router.post(
    "/register",
    response_model=AuthSession,
    status_code=status.HTTP_201_CREATED,
    summary="Create a church user account",
)
async def register(payload: UserCreate, db: DbSession, ip: ClientIp) -> AuthSession:
    if not settings.ALLOW_PUBLIC_REGISTRATION:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Public registration is disabled. Ask an administrator for an account.",
        )

    email = _normalise_email(payload.email)
    existing = await db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email address already exists.",
        )

    user = User(
        email=email,
        full_name=payload.full_name.strip(),
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    await audit.record(
        db,
        action=AuditAction.CREATE,
        entity=AuditEntity.USER,
        entity_id=user.id,
        summary=f"{user.full_name} registered an account",
        actor=user,
        changes={"email": user.email, "is_superuser": user.is_superuser},
        ip_address=ip,
    )
    await db.commit()
    await db.refresh(user)

    tokens = await _issue_tokens(db, user)
    return AuthSession(user=UserRead.model_validate(user), tokens=tokens)


@router.post("/login", response_model=LoginResponse, summary="Sign in with email and password")
async def login(payload: UserLogin, db: DbSession, ip: ClientIp) -> LoginResponse:
    email = _normalise_email(payload.email)
    user = await db.scalar(select(User).where(User.email == email))

    # Constant-ish failure message: never reveal whether the email exists.
    if user is None or not verify_password(payload.password, user.hashed_password):
        # A failed attempt is committed on its own: no other work is pending, and
        # the whole point of the log is to see these - which the raised exception
        # would otherwise roll back. The email is recorded, never the password.
        await audit.record(
            db,
            action=AuditAction.LOGIN_FAILED,
            entity=AuditEntity.AUTH,
            summary=f"Failed sign-in for {email}",
            actor_email=email,
            ip_address=ip,
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
        )
    if not user.is_active:
        await audit.record(
            db,
            action=AuditAction.LOGIN_FAILED,
            entity=AuditEntity.AUTH,
            entity_id=user.id,
            summary=f"Sign-in refused for deactivated account {user.email}",
            actor_email=user.email,
            ip_address=ip,
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated.",
        )

    user.last_login_at = datetime.now(UTC)
    await audit.record(
        db,
        action=AuditAction.LOGIN,
        entity=AuditEntity.AUTH,
        entity_id=user.id,
        summary=f"{user.full_name} signed in",
        actor=user,
        ip_address=ip,
    )
    await db.commit()

    tokens = await _issue_tokens(db, user)
    return LoginResponse(user=UserRead.model_validate(user), tokens=tokens)


@router.post("/refresh", response_model=AuthSession, summary="Exchange a refresh token")
async def refresh(payload: RefreshRequest, db: DbSession) -> AuthSession:
    user = await get_user_for_refresh_token(db, payload.refresh_token)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The refresh token is invalid or has expired.",
        )

    # Rotate: revoke the presented token and issue a fresh pair.
    await revoke_refresh_token(db, payload.refresh_token)
    tokens = await _issue_tokens(db, user)
    return AuthSession(user=UserRead.model_validate(user), tokens=tokens)


@router.post("/logout", response_model=Message, summary="Revoke a refresh token")
async def logout(payload: RefreshRequest, db: DbSession, ip: ClientIp) -> Message:
    # The actor is resolved from the refresh token, *not* from a CurrentUser
    # dependency: sign-out has to keep working when the access token has expired,
    # which is precisely when a user is most likely to press it. An unresolvable
    # token still revokes and still returns 200; only the audit line is skipped.
    actor = await get_user_for_refresh_token(db, payload.refresh_token)
    await revoke_refresh_token(db, payload.refresh_token)
    if actor is not None:
        await audit.record(
            db,
            action=AuditAction.LOGOUT,
            entity=AuditEntity.AUTH,
            entity_id=actor.id,
            summary=f"{actor.full_name} signed out",
            actor=actor,
            ip_address=ip,
        )
        await db.commit()
    return Message(message="Signed out.")


@router.get("/me", response_model=UserRead, summary="Current signed-in user")
async def read_current_user(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)


@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
    summary="Request a password reset link",
)
async def forgot_password(
    payload: ForgotPasswordRequest,
    db: DbSession,
    background_tasks: BackgroundTasks,
) -> ForgotPasswordResponse:
    email = _normalise_email(payload.email)
    user = await db.scalar(select(User).where(User.email == email))

    reset_url: str | None = None
    if user is not None and user.is_active:
        # Invalidate any outstanding reset tokens.
        await db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
        raw_token = generate_opaque_token()
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token(raw_token),
                expires_at=password_reset_expiry(),
            )
        )
        await db.commit()
        background_tasks.add_task(send_password_reset_email, user.email, user.full_name, raw_token)
        reset_url = build_reset_url(raw_token)
        if not settings.RETURN_RESET_LINK_IN_RESPONSE:
            reset_url = None

    # Always the same response so accounts cannot be enumerated.
    return ForgotPasswordResponse(
        message="If the email is registered, a password reset link has been sent.",
        reset_url=reset_url,
    )


@router.post("/reset-password", response_model=Message, summary="Set a new password")
async def reset_password(
    payload: ResetPasswordRequest, db: DbSession, ip: ClientIp
) -> Message:
    result = await db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(payload.token))
    )
    record = result.scalar_one_or_none()
    now = datetime.now(UTC)
    if record is None or record.used_at is not None or record.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This reset link is invalid or has expired. Please request a new one.",
        )

    user = await db.get(User, record.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="This reset link is no longer valid.")

    user.hashed_password = hash_password(payload.new_password)
    record.used_at = now
    # A password change invalidates existing sessions.
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
        summary=f"{user.full_name} reset their password by email link",
        actor=user,
        ip_address=ip,
    )
    await db.commit()
    return Message(message="Your password has been updated. You can now sign in.")


@router.post("/change-password", response_model=Message, summary="Change your own password")
async def change_password(
    payload: ChangePasswordRequest,
    db: DbSession,
    user: CurrentUser,
    ip: ClientIp,
) -> Message:
    if not verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The current password is incorrect.",
        )
    if payload.current_password == payload.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The new password must be different from the current one.",
        )

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
        summary=f"{user.full_name} changed their own password",
        actor=user,
        ip_address=ip,
    )
    await db.commit()
    return Message(message="Password updated.")


@router.get("/config", response_model=dict[str, object], summary="Client auth configuration")
async def auth_config(request: Request) -> dict[str, object]:
    return {
        "allow_public_registration": settings.ALLOW_PUBLIC_REGISTRATION,
        "password_min_length": 8,
        "return_reset_link_in_response": settings.RETURN_RESET_LINK_IN_RESPONSE,
    }
