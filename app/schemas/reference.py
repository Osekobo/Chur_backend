"""Reference data exposed to the frontend (single source of truth)."""

from __future__ import annotations

from pydantic import BaseModel

from app.enums import (
    EXPENSE_CATEGORIES,
    INCOME_CATEGORIES,
    TRANSFER_CATEGORY,
    Account,
    Fund,
    MemberCategory,
    PersonRole,
    TransactionType,
)


class ReferenceData(BaseModel):
    """Everything the UI needs to render its forms and menus."""

    person_roles: list[str]
    member_categories: list[str]
    accounts: list[str]
    funds: list[str]
    income_categories: list[str]
    expense_categories: list[str]
    transfer_category: str
    transaction_types: list[str]


def build_reference_data() -> ReferenceData:
    return ReferenceData(
        person_roles=[role.value for role in PersonRole],
        member_categories=[category.value for category in MemberCategory],
        accounts=[account.value for account in Account],
        funds=[fund.value for fund in Fund],
        income_categories=list(INCOME_CATEGORIES),
        expense_categories=list(EXPENSE_CATEGORIES),
        transfer_category=TRANSFER_CATEGORY,
        transaction_types=[t.value for t in TransactionType],
    )
