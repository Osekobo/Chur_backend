"""Dashboard, report and double-entry accounting endpoints."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, DbSession
from app.core.permissions import require_permission
from app.db.models import Person, Transaction, User
from app.enums import (
    ACCOUNTS,
    EXPENSE_CATEGORIES,
    FUNDS,
    INCOME_CATEGORIES,
    TRANSFER_CATEGORY,
    Permission,
    PersonRole,
    TransactionType,
)
from app.schemas.reference import ReferenceData, build_reference_data
from app.schemas.transaction import (
    AccountSummary,
    CategoryTotal,
    ChartOfAccounts,
    ContributionSearchResult,
    DashboardSummary,
    FinancialStatements,
    MemberContribution,
    NamedTotal,
    SummaryReport,
    TransactionRead,
    TrialBalance,
)
from app.services import accounting
from app.services.accounting import sum_money

router = APIRouter(tags=["reporting"])

RECENT_TRANSACTION_LIMIT = 8

#: Three levels of reading, because they answer different questions. The
#: dashboard is a glance anyone may have; the giving reports and the double-entry
#: statements are the accountant's working figures. A secretary reads the first
#: two but not the statements - there is nothing in them about collections she
#: could act on.
DashboardViewer = Annotated[User, Depends(require_permission(Permission.DASHBOARD_VIEW))]
ReportReader = Annotated[User, Depends(require_permission(Permission.REPORTS_VIEW))]
AccountingReader = Annotated[User, Depends(require_permission(Permission.ACCOUNTING_VIEW))]


async def _all_transactions(db: AsyncSession) -> list[Transaction]:
    return list((await db.execute(select(Transaction))).scalars().all())


@router.get("/dashboard", response_model=DashboardSummary, summary="Dashboard KPIs")
async def dashboard(db: DbSession, _user: DashboardViewer) -> DashboardSummary:
    transactions = await _all_transactions(db)
    income = accounting.income_total(transactions)
    expenses = accounting.expense_total(transactions)

    people_counts = (
        await db.execute(select(Person.role, func.count()).group_by(Person.role))
    ).all()
    counts = {str(role): int(count) for role, count in people_counts}

    recent = (
        (
            await db.execute(
                select(Transaction)
                .where(Transaction.category != TRANSFER_CATEGORY)
                .order_by(Transaction.date.desc(), Transaction.created_at.desc())
                .limit(RECENT_TRANSACTION_LIMIT)
            )
        )
        .scalars()
        .all()
    )

    return DashboardSummary(
        total_income=income,
        total_expenses=expenses,
        net_balance=(income - expenses).quantize(Decimal("0.01")),
        member_count=counts.get(PersonRole.MEMBER.value, 0),
        person_count=sum(counts.values()),
        transaction_count=len(transactions),
        balance_by_account=accounting.balance_by_account(transactions),
        net_by_fund=accounting.net_by_fund(transactions),
        income_by_category=accounting.totals_by_category(
            transactions, INCOME_CATEGORIES, TransactionType.INCOME
        ),
        expense_by_category=accounting.totals_by_category(
            transactions, EXPENSE_CATEGORIES, TransactionType.EXPENSE
        ),
        recent_transactions=[TransactionRead.model_validate(tx) for tx in recent],
    )


@router.get("/reports/summary", response_model=SummaryReport, summary="All-time category totals")
async def summary_report(db: DbSession, _user: ReportReader) -> SummaryReport:
    transactions = await _all_transactions(db)
    income = accounting.income_total(transactions)
    expenses = accounting.expense_total(transactions)
    return SummaryReport(
        income_by_category=accounting.totals_by_category(
            transactions, INCOME_CATEGORIES, TransactionType.INCOME
        ),
        expense_by_category=accounting.totals_by_category(
            transactions, EXPENSE_CATEGORIES, TransactionType.EXPENSE
        ),
        total_income=income,
        total_expenses=expenses,
        net_balance=(income - expenses).quantize(Decimal("0.01")),
        transaction_count=len(transactions),
    )


@router.get(
    "/reports/member-contributions",
    response_model=list[str],
    summary="Names that can be searched in contribution history",
)
async def contribution_names(db: DbSession, _user: ReportReader) -> list[str]:
    rows = (
        (
            await db.execute(
                select(Transaction.party)
                .where(
                    Transaction.type == TransactionType.INCOME,
                    Transaction.category != TRANSFER_CATEGORY,
                    Transaction.party != "",
                )
                .distinct()
                .order_by(Transaction.party)
            )
        )
        .scalars()
        .all()
    )
    return [str(name) for name in rows]


@router.get(
    "/reports/contributions",
    response_model=ContributionSearchResult,
    summary="Search contributions by name (partial match)",
)
async def search_contributions(
    db: DbSession,
    _user: ReportReader,
    search: Annotated[str, Query(min_length=1, max_length=200)],
) -> ContributionSearchResult:
    """Every Money In entry recorded under a name containing ``search``.

    Matching is case-insensitive and partial, so "otieno" finds
    "John Otieno" and "Mary Atieno Otieno" alike. Transfers are excluded:
    they carry no contributor and are not contributions.
    """
    term = search.strip()
    pattern = f"%{term.casefold()}%"

    rows = (
        (
            await db.execute(
                select(Transaction)
                .where(
                    Transaction.type == TransactionType.INCOME,
                    Transaction.category != TRANSFER_CATEGORY,
                    func.lower(Transaction.party).like(pattern),
                )
                .order_by(Transaction.date.desc(), Transaction.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    return ContributionSearchResult(
        search=term,
        count=len(rows),
        total=accounting.sum_money(row.amount for row in rows),
        transactions=[TransactionRead.model_validate(row) for row in rows],
    )


@router.get(
    "/reports/member-contributions/detail",
    response_model=list[MemberContribution],
    summary="Contribution history for one member",
)
async def member_contributions(
    db: DbSession,
    _user: ReportReader,
    name: Annotated[str, Query(min_length=1, max_length=200)],
) -> list[MemberContribution]:
    """Full history for an exact contributor name, grouped per person.

    Match the name case-insensitively but ignore surrounding whitespace, so
    "  john " and "John" are the same person. Use
    ``/reports/contributions`` to search when only part of the name is known.
    """
    transactions = await _all_transactions(db)
    wanted = name.strip().casefold()

    grouped: dict[str, list[Transaction]] = {}
    for tx in transactions:
        if tx.type != TransactionType.INCOME or tx.category == TRANSFER_CATEGORY:
            continue
        party = (tx.party or "").strip()
        if not party:
            continue
        grouped.setdefault(party.casefold(), []).append(tx)

    rows = grouped.get(wanted, [])
    if not rows:
        return []

    rows.sort(key=lambda tx: tx.date, reverse=True)
    by_category = [
        CategoryTotal(
            category=category,
            total=sum_money(tx.amount for tx in rows if tx.category == category),
        )
        for category in INCOME_CATEGORIES
    ]
    by_category = [item for item in by_category if item.total > 0]

    return [
        MemberContribution(
            member=rows[0].party.strip(),
            total=sum_money(tx.amount for tx in rows),
            contribution_count=len(rows),
            by_category=by_category,
            transactions=[TransactionRead.model_validate(tx) for tx in rows],
        )
    ]


@router.get("/accounting/ledger", response_model=list[TransactionRead], summary="General ledger")
async def general_ledger(
    db: DbSession,
    _user: AccountingReader,
    include_transfers: bool = True,
) -> list[TransactionRead]:
    statement = select(Transaction)
    if not include_transfers:
        statement = statement.where(Transaction.category != TRANSFER_CATEGORY)
    rows = (
        (await db.execute(statement.order_by(Transaction.date.asc(), Transaction.created_at.asc())))
        .scalars()
        .all()
    )
    return [TransactionRead.model_validate(tx) for tx in rows]


@router.get(
    "/accounting/chart-of-accounts",
    response_model=ChartOfAccounts,
    summary="Chart of accounts with balances",
)
async def chart_of_accounts(db: DbSession, _user: AccountingReader) -> ChartOfAccounts:
    transactions = await _all_transactions(db)
    accounts, total_debits, total_credits = accounting.trial_balance(transactions)
    return ChartOfAccounts(
        accounts=accounts,
        total_debits=total_debits,
        total_credits=total_credits,
        is_balanced=abs(total_debits - total_credits) < Decimal("0.005"),
    )


@router.get(
    "/accounting/trial-balance",
    response_model=TrialBalance,
    summary="Trial balance",
)
async def trial_balance(db: DbSession, _user: AccountingReader) -> TrialBalance:
    transactions = await _all_transactions(db)
    accounts, total_debits, total_credits = accounting.trial_balance(transactions)
    return TrialBalance(
        accounts=accounts,
        total_debits=total_debits,
        total_credits=total_credits,
        is_balanced=abs(total_debits - total_credits) < Decimal("0.005"),
        difference=(total_debits - total_credits).quantize(Decimal("0.01")),
    )


@router.get(
    "/accounting/financial-statements",
    response_model=FinancialStatements,
    summary="Income statement and balance sheet position",
)
async def financial_statements(db: DbSession, _user: AccountingReader) -> FinancialStatements:
    transactions = await _all_transactions(db)
    lines = accounting.journal_lines(transactions)

    income = accounting.income_total(transactions)
    expenses = accounting.expense_total(transactions)

    account_position = [
        AccountSummary(
            account=account,
            account_type=accounting.account_type(account).value,
            debit=accounting.sum_money(line.debit for line in lines if line.account == account),
            credit=accounting.sum_money(line.credit for line in lines if line.account == account),
            balance=accounting.account_balance(lines, account),
        )
        for account in ACCOUNTS
    ]
    fund_position: list[NamedTotal] = []
    for fund in FUNDS:
        received, spent = accounting.fund_position(transactions, fund)
        fund_position.append(NamedTotal(key=fund, total=received - spent))

    return FinancialStatements(
        income=accounting.totals_by_category(
            transactions, INCOME_CATEGORIES, TransactionType.INCOME
        ),
        expenses=accounting.totals_by_category(
            transactions, EXPENSE_CATEGORIES, TransactionType.EXPENSE
        ),
        total_income=income,
        total_expenses=expenses,
        net_surplus=(income - expenses).quantize(Decimal("0.01")),
        account_position=account_position,
        total_assets=sum_money(item.balance for item in account_position),
        fund_position=fund_position,
    )


@router.get("/reference", response_model=ReferenceData, summary="Reference data for forms")
async def reference(_user: CurrentUser) -> ReferenceData:
    return build_reference_data()
