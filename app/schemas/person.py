"""Person (directory) schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from app.enums import MemberCategory, PersonRole
from app.schemas.common import ORMModel


class PersonBase(BaseModel):
    role: PersonRole
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(default="", max_length=40)
    category: str = Field(default="", max_length=60)
    notes: str = Field(default="", max_length=2000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Name cannot be blank.")
        return value

    @field_validator("category")
    @classmethod
    def _validate_category(cls, value: str, info: ValidationInfo) -> str:
        value = value.strip()
        role = info.data.get("role")
        if value and role == PersonRole.MEMBER and value not in MemberCategory:
            allowed = ", ".join(c.value for c in MemberCategory)
            raise ValueError(f"Category must be one of: {allowed}.")
        return value


class PersonCreate(PersonBase):
    pass


class PersonUpdate(BaseModel):
    """Partial update; unset fields are left untouched."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    category: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=2000)
    role: PersonRole | None = None


class PersonRead(PersonBase, ORMModel):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
