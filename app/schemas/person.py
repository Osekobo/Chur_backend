"""Person (directory) schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from app.enums import EMAIL_REQUIRED_ROLES, MemberCategory, PersonRole
from app.schemas.auth import EMAIL_PATTERN
from app.schemas.common import ORMModel


def clean_email(value: str) -> str:
    """Trim an address and check its shape, allowing an empty one."""
    value = value.strip()
    if value and not EMAIL_PATTERN.match(value):
        raise ValueError("That does not look like an email address.")
    return value


def check_email_required(role: PersonRole, email: str) -> None:
    """Enforce that the roles we always email actually carry an address.

    Called from both the create model and the patch route, because the rule spans
    two fields: flipping somebody from Member to Supplier has to demand the
    address that the Member record did not need.
    """
    if role in EMAIL_REQUIRED_ROLES and not email.strip():
        raise ValueError(f"An email address is required for a {role.value.lower()}.")


class PersonBase(BaseModel):
    role: PersonRole
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(default="", max_length=40)
    #: Optional for members and guests; see check_email_required for the rest.
    email: str = Field(default="", max_length=320)
    category: str = Field(default="", max_length=60)
    notes: str = Field(default="", max_length=2000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Name cannot be blank.")
        return value

    _clean_email = field_validator("email")(clean_email)

    @field_validator("category")
    @classmethod
    def _validate_category(cls, value: str, info: ValidationInfo) -> str:
        value = value.strip()
        role = info.data.get("role")
        if value and role == PersonRole.MEMBER and value not in MemberCategory:
            allowed = ", ".join(c.value for c in MemberCategory)
            raise ValueError(f"Category must be one of: {allowed}.")
        return value

    @model_validator(mode="after")
    def _email_matches_role(self) -> PersonBase:
        check_email_required(self.role, self.email)
        return self


class PersonCreate(PersonBase):
    pass


class PersonUpdate(BaseModel):
    """Partial update; unset fields are left untouched.

    Carries no validators of its own on purpose: every rule that spans two fields
    (email against role, category against role) needs the *merged* record, so the
    route re-validates the whole person through ``PersonBase`` before saving.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=320)
    category: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=2000)
    role: PersonRole | None = None


class PersonRead(PersonBase, ORMModel):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime