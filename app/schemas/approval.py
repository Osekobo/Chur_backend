"""Money-out approval workflow schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, computed_field, field_validator

from app.enums import Account, ApprovalStatus, ExpenseCategory, Fund
from app.schemas.common import ORMModel

MONEY = Decimal


class ApprovalBase(BaseModel):
    """The money-out detail a request asks for.

    Mirrors :class:`app.schemas.transaction.TransactionBase` for the fields that
    matter, so a request cannot be raised for a combination the ledger would
    later reject.
    """

    category: str = Field(min_length=1, max_length=60)
    party: str = Field(default="", max_length=200)
    amount: MONEY = Field(gt=0, max_digits=14, decimal_places=2)
    fund: str = Field(default="", max_length=60)
    account: Account
    notes: str = Field(default="", max_length=2000)
    date: date

    @field_validator("amount")
    @classmethod
    def _round_amount(cls, value: MONEY) -> MONEY:
        return value.quantize(Decimal("0.01"))

    @field_validator("party", "notes", "fund")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("category")
    @classmethod
    def _check_expense_category(cls, value: str) -> str:
        if value not in ExpenseCategory:
            allowed = ", ".join(c.value for c in ExpenseCategory)
            raise ValueError(f"Category must be one of: {allowed}.")
        return value

    @field_validator("fund")
    @classmethod
    def _check_fund(cls, value: str) -> str:
        if value and value not in Fund:
            raise ValueError(f"Fund must be one of: {', '.join(f.value for f in Fund)}.")
        return value


class ApprovalCreate(ApprovalBase):
    """Raise a new request. It starts pending and invisible to the ledger."""


class ApprovalRead(ORMModel):
    id: uuid.UUID
    status: ApprovalStatus
    category: str
    party: str
    amount: MONEY
    fund: str
    account: Account
    notes: str
    date: date
    requested_by_id: uuid.UUID | None = None
    decided_by_id: uuid.UUID | None = None
    decided_at: datetime | None = None
    decision_note: str
    transaction_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_pending(self) -> bool:
        return self.status is ApprovalStatus.PENDING


class ApprovalDecision(BaseModel):
    """An administrator's verdict on a request."""

    note: str = Field(default="", max_length=500)

    @field_validator("note")
    @classmethod
    def _strip_note(cls, value: str) -> str:
        return value.strip()


class ApprovalSummary(BaseModel):
    """Counts for the sidebar badge, so it does not need the full list."""

    pending: int = 0
    approved: int = 0
    rejected: int = 0
    total: int = 0