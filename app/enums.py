"""Domain enumerations shared by models, schemas and services."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal


class PersonRole(StrEnum):
    """Role of a person record in the church directory."""

    MEMBER = "Member"
    #: Someone who attends but is not on the membership roll - a regular
    #: collection donor. Distinct from MEMBER so the counts stay honest, and kept
    #: next to it so the directory tabs read members and guests together.
    GUEST = "Guest"
    SUPPLIER = "Supplier"
    EMPLOYEE = "Employee"
    USER = "User"

    @property
    def label(self) -> str:
        return f"{self.value}s"


#: Directory roles that are always contacted by email, so the field is mandatory
#: for them and optional for everyone else (Members and Guests usually have no
#: address to give).
EMAIL_REQUIRED_ROLES: frozenset[PersonRole] = frozenset(
    {PersonRole.SUPPLIER, PersonRole.EMPLOYEE, PersonRole.USER}
)


class MemberCategory(StrEnum):
    """Sub grouping used for members."""

    MEN = "Men"
    WOMEN = "Women"
    YOUTH = "Youth"
    SUNDAY_SCHOOL = "Sunday School"


class TransactionType(StrEnum):
    """Direction of a transaction relative to the church."""

    INCOME = "income"
    EXPENSE = "expense"


class Account(StrEnum):
    """Payment / holding accounts (asset accounts)."""

    CASH = "Cash"
    BANK = "Bank"
    M_PESA = "M-PESA"


#: The extra row in a Tithe % Deduction account dropdown: "the tithes collected
#: that day, whichever account they came into". Paired with the ``Account`` values
#: to make the dropdown four rows long without a parallel enum to keep in step.
TITHE_SCOPE_ALL = "all"

#: Reads in the 422 raised when the chosen scope has no tithes to work on.
TITHE_SCOPE_ALL_LABEL = "any account"

#: What the deduction field accepts: one account, or every account. The literal
#: has to be spelled out here for mypy, so a test asserts it matches
#: ``TITHE_SCOPE_ALL`` rather than letting the two drift apart silently.
TitheScope = Account | Literal["all"]


class Fund(StrEnum):
    """Restricted or designated funds."""

    GENERAL = "General Fund"
    BUILDING = "Building"
    #: One fund per congregation grouping that collects and spends separately.
    YOUTH = "Youth"
    MEN = "Men"
    WOMEN = "Women"
    SUNDAY_SCHOOL = "Sunday School"
    MISSIONS = "Missions"
    OTHER = "Other"


class IncomeCategory(StrEnum):
    """Revenue accounts."""

    TITHES = "Tithes"
    OFFERINGS = "Offerings"
    DONATIONS = "Donations"
    PLEDGES = "Pledges"
    WELFARE = "Welfare"
    OTHER_INCOME = "Other Income"


class ExpenseCategory(StrEnum):
    """Expense accounts."""

    EXPENSES = "Expenses"
    PETTY_CASH = "Petty Cash"
    SUPPLIERS = "Suppliers"
    PAYMENTS = "Payments"
    #: Produced by the "Tithe % Deduction" page, e.g. a diocese remittance.
    PERCENTAGE_DEDUCTION = "Percentage Deduction"


#: Sentinel category used by paired transfer rows.
TRANSFER_CATEGORY = "Transfer"

#: The category written by the Tithe % Deduction page.
PCT_DEDUCTION_CATEGORY = ExpenseCategory.PERCENTAGE_DEDUCTION.value

#: Percentage deductions always land here - they come off the whole collection,
#: not out of a designated project.
FUND_DEDUCTION = Fund.GENERAL.value

INCOME_CATEGORIES: tuple[str, ...] = tuple(c.value for c in IncomeCategory)
EXPENSE_CATEGORIES: tuple[str, ...] = tuple(c.value for c in ExpenseCategory)
ACCOUNTS: tuple[str, ...] = tuple(a.value for a in Account)
FUNDS: tuple[str, ...] = tuple(f.value for f in Fund)
ALL_CATEGORIES: tuple[str, ...] = INCOME_CATEGORIES + EXPENSE_CATEGORIES + (TRANSFER_CATEGORY,)


class AccountType(StrEnum):
    """Classification of an account in the chart of accounts."""

    ASSET = "Asset"
    INCOME = "Income"
    EXPENSE = "Expense"
    OTHER = "Other"


# --------------------------------------------------------------------------- #
# Audit trail
# --------------------------------------------------------------------------- #
class AuditAction(StrEnum):
    """What was done, in the past tense so the log reads as a sentence."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    ROLE_CHANGE = "role_change"
    PASSWORD_RESET = "password_reset"
    ACCOUNT_DEACTIVATED = "account_deactivated"
    ACCOUNT_REACTIVATED = "account_reactivated"
    APPROVE = "approve"
    REJECT = "reject"


class AuditEntity(StrEnum):
    """Which kind of record the entry is about."""

    TRANSACTION = "transaction"
    PERSON = "person"
    USER = "user"
    AUTH = "auth"
    APPROVAL = "approval"


class ApprovalStatus(StrEnum):
    """Where a money-out request sits in the approval workflow."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# --------------------------------------------------------------------------- #
# Roles and permissions
# --------------------------------------------------------------------------- #
class UserRole(StrEnum):
    """What a signed-in user is there to do. One role per user.

    Replaces the old ``is_superuser`` flag: there was no way to say "handles the
    money but not the user accounts", which is most of the church office.
    """

    #: Records money in and out, moves money between accounts, runs the
    #: accounting reports, and decides approval requests.
    ACCOUNTANT = "accountant"
    #: Keeps the directory and records money coming in; reads reports but does
    #: not spend, transfer, or decide requests.
    SECRETARY = "secretary"
    #: Everything an accountant can do, plus managing accounts and the audit trail.
    ADMIN = "admin"

    @property
    def label(self) -> str:
        """Capitalised name for the interface."""
        return {"accountant": "Accountant", "secretary": "Secretary", "admin": "Administrator"}[
            self.value
        ]


class Permission(StrEnum):
    """A single thing a role may be allowed to do.

    Endpoints ask for one of these instead of testing flags, so the access rules
    live in one place (see ``app/core/permissions.py``) rather than scattered
    across route signatures.
    """

    DASHBOARD_VIEW = "dashboard:view"
    #: Read the directory; what the picker on the Money In pages needs.
    PEOPLE_VIEW = "people:view"
    #: Add, edit and delete directory records.
    PEOPLE_MANAGE = "people:manage"
    #: Record money coming in.
    MONEY_IN = "money:in"
    #: Record money going out.
    MONEY_OUT = "money:out"
    #: Move money between the church's own accounts.
    TRANSFERS_MANAGE = "transfers:manage"
    #: Record the Tithe % Deduction.
    DEDUCTIONS_MANAGE = "deductions:manage"
    #: Ledger, chart of accounts, trial balance, financial statements, per-account
    #: and per-fund views.
    ACCOUNTING_VIEW = "accounting:view"
    REPORTS_VIEW = "reports:view"
    APPROVALS_VIEW = "approvals:view"
    #: Ask for money out.
    APPROVALS_REQUEST = "approvals:request"
    #: Approve or reject someone else's request.
    APPROVALS_DECIDE = "approvals:decide"
    #: Provision accounts, reset passwords, force sign-outs.
    USERS_MANAGE = "users:manage"
    AUDIT_VIEW = "audit:view"

    @property
    def activity(self) -> str:
        """What the permission is *for*, as a verb phrase.

        Used to tell the user why they were refused ("an accountant cannot decide
        approval requests"), which is worth more than echoing the internal name.
        """
        return {
            "dashboard:view": "see the dashboard",
            "people:view": "look people up",
            "people:manage": "change the directory",
            "money:in": "record money in",
            "money:out": "record money out",
            "transfers:manage": "move money between accounts",
            "deductions:manage": "run the Tithe % Deduction",
            "accounting:view": "read the accounting reports",
            "reports:view": "read the reports",
            "approvals:view": "see approval requests",
            "approvals:request": "ask for money out",
            "approvals:decide": "decide approval requests",
            "users:manage": "manage user accounts",
            "audit:view": "read the audit trail",
        }[self.value]
