"""Authentication and user schemas."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, computed_field, field_validator

from app.core.permissions import permissions_for
from app.enums import UserRole
from app.schemas.common import ORMModel, TokenPair

PASSWORD_MIN_LENGTH = 8
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_password(value: str) -> str:
    if len(value) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters long.")
    if not any(c.isalpha() for c in value):
        raise ValueError("Password must contain at least one letter.")
    if not any(c.isdigit() for c in value):
        raise ValueError("Password must contain at least one number.")
    return value


class UserBase(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)


class UserCreate(UserBase):
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)

    _check_password = field_validator("password")(_validate_password)

    @field_validator("full_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Full name cannot be blank.")
        return value


class UserLogin(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10)


class UserRead(ORMModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    is_active: bool
    role: UserRole
    last_login_at: datetime | None = None
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def role_label(self) -> str:
        """Capitalised name for the interface.

        Derived rather than stored so the database stays the single source of truth
        and the client never has to format the value itself.
        """
        if not self.is_active:
            return "Deactivated"
        return self.role.label

    @computed_field  # type: ignore[prop-decorator]
    @property
    def permissions(self) -> list[str]:
        """What this account may do, so the interface can hide the rest.

        The server checks every one of these again on each request; the list only
        spares the user from being offered a page that would refuse them.
        """
        return sorted(permission.value for permission in permissions_for(self.role))


class AuthSession(BaseModel):
    """Payload returned by login / register / refresh."""

    user: UserRead
    tokens: TokenPair


class LoginResponse(AuthSession):
    pass


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    message: str
    reset_url: str | None = Field(
        default=None,
        description="Only populated when RETURN_RESET_LINK_IN_RESPONSE is enabled (development).",
    )


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)

    _check_password = field_validator("new_password")(_validate_password)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)

    _check_password = field_validator("new_password")(_validate_password)


# --------------------------------------------------------------------------- #
# Administrator-only payloads (POST/PATCH /users)
# --------------------------------------------------------------------------- #
class AdminUserCreate(UserCreate):
    """An administrator provisions this account, so the role is chosen up front.

    Reuses ``UserCreate`` so the password rules and name trimming stay identical
    to public registration - the only difference is that the role is settable here.
    The default is the least privileged money role, so forgetting to choose one
    cannot hand out account management by accident.
    """

    role: UserRole = UserRole.ACCOUNTANT


class UserUpdate(BaseModel):
    """Partial update of an account. Omitted fields are left untouched."""

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    is_active: bool | None = None
    role: UserRole | None = None

    @field_validator("full_name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Full name cannot be blank.")
        return stripped


class SetPasswordRequest(BaseModel):
    """An administrator sets a user's password directly, without the old one."""

    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)

    _check_password = field_validator("new_password")(_validate_password)


__all__ = [
    "EMAIL_PATTERN",
    "AdminUserCreate",
    "AuthSession",
    "ChangePasswordRequest",
    "ForgotPasswordRequest",
    "ForgotPasswordResponse",
    "LoginResponse",
    "RefreshRequest",
    "ResetPasswordRequest",
    "SetPasswordRequest",
    "UserCreate",
    "UserLogin",
    "UserRead",
    "UserUpdate",
]
