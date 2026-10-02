"""Transaction, transfer and reporting schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.enums import (
    PCT_DEDUCTION_CATEGORY,
    TRANSFER_CATEGORY,
    Account,
    DeductionBasis,
    ExpenseCategory,
    Fund,
    IncomeCategory,
    TransactionType,
)
from app.schemas.common import ORMModel

MONEY = Decimal


class TransactionBase(BaseModel):
    type: TransactionType
    category: str = Field(min_length=1, max_length=60)
    party: str = Field(default="", max_length=200)
    amount: MONEY = Field(gt=0, max_digits=14, decimal_places=2)
    fund: str = Field(default="", max_length=60)
    account: Account
    notes: str = Field(default="", max_length=2000)
    date: date
    #: Only meaningful for a "Percentage Deduction" entry.
    pct: MONEY | None = Field(default=None, ge=0, le=100, max_digits=6, decimal_places=2)
    #: The tithe total the percentage was taken from.
    base_total: MONEY | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)

    @field_validator("amount")
    @classmethod
    def _round_amount(cls, value: MONEY) -> MONEY:
        return value.quantize(Decimal("0.01"))

    @field_validator("party", "notes", "fund")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _check_pct_pairing(self) -> TransactionBase:
        if (self.pct is None) != (self.base_total is None):
            raise ValueError("A percentage deduction needs both a percentage and a base total.")
        if self.pct is not None and self.category != PCT_DEDUCTION_CATEGORY:
            raise ValueError("A percentage can only be recorded on a Percentage Deduction.")
        return self

    @model_validator(mode="after")
    def _check_category_matches_type(self) -> TransactionBase:
        if self.category == TRANSFER_CATEGORY:
            raise ValueError("Use the transfer endpoint to move money between accounts.")
        if self.type is TransactionType.INCOME and self.category not in IncomeCategory:
            raise ValueError("Income must use one of the income categories.")
        if self.type is TransactionType.EXPENSE and self.category not in ExpenseCategory:
            raise ValueError("Expenses must use one of the expense categories.")
        if self.category not in IncomeCategory and self.category not in ExpenseCategory:
            raise ValueError("Unknown category.")
        if self.fund and self.fund not in Fund:
            raise ValueError(f"Fund must be one of: {', '.join(f.value for f in Fund)}.")
        return self


class TransactionCreate(TransactionBase):
    """Write model — validated, and only accepts real income/expense entries."""


class TransactionRead(ORMModel):
    """Read model — mirrors the stored row, including transfer legs.

    Deliberately does not inherit ``TransactionBase``: stored transfer rows use
    the sentinel ``Transfer`` category that writes are not allowed to produce.
    """

    id: uuid.UUID
    type: TransactionType
    category: str
    party: str
    amount: MONEY
    fund: str
    account: Account
    notes: str
    date: date
    transfer_id: uuid.UUID | None = None
    pct: MONEY | None = None
    base_total: MONEY | None = None
    created_by_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class TransferCreate(BaseModel):
    from_account: Account
    to_account: Account
    amount: MONEY = Field(gt=0, max_digits=14, decimal_places=2)
    date: date
    notes: str = Field(default="", max_length=2000)

    @field_validator("amount")
    @classmethod
    def _round_amount(cls, value: MONEY) -> MONEY:
        return value.quantize(Decimal("0.01"))

    @model_validator(mode="after")
    def _distinct_accounts(self) -> TransferCreate:
        if self.from_account is self.to_account:
            raise ValueError("Choose two different accounts.")
        return self


class TransferRead(BaseModel):
    transfer_id: uuid.UUID
    date: date
    from_account: Account
    to_account: Account
    amount: MONEY
    notes: str = ""


# --------------------------------------------------------------------------- #
# Reporting / accounting
# --------------------------------------------------------------------------- #
class JournalLine(BaseModel):
    date: date
    account: str
    debit: MONEY = Decimal("0.00")
    credit: MONEY = Decimal("0.00")
    transaction_id: uuid.UUID | None = None
    description: str = ""


class AccountSummary(BaseModel):
    account: str
    account_type: str
    debit: MONEY
    credit: MONEY
    balance: MONEY


class ChartOfAccounts(BaseModel):
    accounts: list[AccountSummary]
    total_debits: MONEY
    total_credits: MONEY
    is_balanced: bool


class TrialBalance(ChartOfAccounts):
    difference: MONEY


class CategoryTotal(BaseModel):
    category: str
    total: MONEY


class NamedTotal(BaseModel):
    key: str
    total: MONEY


class FundSummary(BaseModel):
    """Received, spent and net for one fund, aggregated by the server.

    The fund screen needs all three figures; ``/transactions/funds/net`` alone
    cannot supply the first two without the client downloading the whole ledger.
    """

    fund: str
    received: MONEY
    spent: MONEY
    net: MONEY


class DashboardSummary(BaseModel):
    total_income: MONEY
    total_expenses: MONEY
    net_balance: MONEY
    member_count: int
    person_count: int
    transaction_count: int
    balance_by_account: list[NamedTotal]
    net_by_fund: list[NamedTotal]
    income_by_category: list[CategoryTotal]
    expense_by_category: list[CategoryTotal]
    recent_transactions: list[TransactionRead]


class MemberContribution(BaseModel):
    member: str
    total: MONEY
    contribution_count: int
    by_category: list[CategoryTotal]
    transactions: list[TransactionRead]


class ContributionSearchResult(BaseModel):
    """Flat result of a partial name search, as shown on the Reports page."""

    search: str
    count: int
    total: MONEY
    transactions: list[TransactionRead]


class SummaryReport(BaseModel):
    income_by_category: list[CategoryTotal]
    expense_by_category: list[CategoryTotal]
    total_income: MONEY
    total_expenses: MONEY
    net_balance: MONEY
    transaction_count: int


class FinancialStatements(BaseModel):
    """Income statement plus the current cash / account position."""

    income: list[CategoryTotal]
    expenses: list[CategoryTotal]
    total_income: MONEY
    total_expenses: MONEY
    net_surplus: MONEY
    account_position: list[AccountSummary]
    total_assets: MONEY
    fund_position: list[NamedTotal]


# --------------------------------------------------------------------------- #
# Tithe % deduction
# --------------------------------------------------------------------------- #
class TithesOnDate(BaseModel):
    """Tithes collected on a single date - the base a percentage is taken from."""

    date: date
    total: MONEY = Decimal("0.00")
    entry_count: int = 0


class PctDeductionCreate(BaseModel):
    """Deduct a percentage of one Sunday's collection, e.g. a diocese remittance.

    ``basis`` decides what the percentage is taken from; it defaults to the
    tithe collection alone so an older client keeps its original behaviour.
    """

    date: date
    pct: MONEY = Field(gt=0, le=100, max_digits=6, decimal_places=2)
    account: Account
    notes: str = Field(default="", max_length=2000)
    basis: DeductionBasis = DeductionBasis.TITHES

    @field_validator("pct")
    @classmethod
    def _round_pct(cls, value: MONEY) -> MONEY:
        return value.quantize(Decimal("0.01"))

    @field_validator("notes")
    @classmethod
    def _strip_notes(cls, value: str) -> str:
        return value.strip()


class PctDeductionResult(BaseModel):
    """The created expense entry, with the arithmetic that produced it."""

    transaction: TransactionRead
    #: The collection the percentage was taken from, for the chosen basis.
    collected_that_day: MONEY
    deduction_amount: MONEY
    basis: DeductionBasis = DeductionBasis.TITHES


class PctDeductionPreview(BaseModel):
    """Live preview for the form: what the deduction would come to."""

    date: date
    pct: MONEY
    collected_that_day: MONEY = Decimal("0.00")
    deduction_amount: MONEY = Decimal("0.00")
    basis: DeductionBasis = DeductionBasis.TITHES
