"""Password hashing, token creation and token verification helpers."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.core.config import settings

# Argon2id password hashing.
password_hash = PasswordHash((Argon2Hasher(),))

TokenType = Literal["access"]


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired or invalid."""


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(plain_password: str) -> str:
    return password_hash.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return password_hash.verify(plain_password, hashed_password)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Opaque tokens (refresh tokens, password reset tokens)
# --------------------------------------------------------------------------- #
def generate_opaque_token() -> str:
    """Cryptographically strong, URL-safe token."""
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """Tokens are stored as SHA-256 digests so a DB leak cannot be replayed."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# JWT access tokens
# --------------------------------------------------------------------------- #
def _create_token(subject: str | uuid.UUID, expires_delta: timedelta, token_type: TokenType) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "type": token_type,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_access_token(
    subject: str | uuid.UUID,
    expires_minutes: int | None = None,
) -> str:
    minutes = expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES
    return _create_token(subject, timedelta(minutes=minutes), "access")


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT access token.

    Raises:
        TokenError: if the token is expired, tampered with or of the wrong type.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("The access token has expired.") from exc
    except jwt.PyJWTError as exc:
        raise TokenError("The access token is invalid.") from exc

    if payload.get("type") != "access":
        raise TokenError("The access token is invalid.")
    if not payload.get("sub"):
        raise TokenError("The access token is invalid.")
    return payload


def refresh_token_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)


def password_reset_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=settings.PASSWORD_RESET_EXPIRE_MINUTES)
