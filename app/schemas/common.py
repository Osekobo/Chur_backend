"""Shared schema helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    """Base schema configured to read values straight from ORM objects."""

    model_config = ConfigDict(from_attributes=True)


class Message(BaseModel):
    """Simple acknowledgement payload."""

    message: str


class ErrorDetail(BaseModel):
    detail: str


class Page(BaseModel, Generic[T]):
    """Paginated collection envelope."""

    items: list[T]
    total: int
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    pages: int = Field(ge=0)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds.")


class TimestampedRead(ORMModel):
    created_at: datetime
    updated_at: datetime
