"""Double-entry accounting engine.

Every helper in this module is a pure function over a list of transaction
records so the accounting rules can be unit tested without a database.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from app.enums import (
    ACCOUNTS,
    EXPENSE_CATEGORIES,
    FUNDS,
    INCOME_CATEGORIES,
    TRANSFER_CATEGORY,
    AccountType,
    IncomeCategory,
)
from app.schemas.transaction import (
    AccountSummary,
    CategoryTotal,
    JournalLine,
    NamedTotal,
)

ZERO = Decimal("0.00")


class TransactionLike(Protocol):
    """Structural type shared by ORM rows and test doubles."""

    id: Any
    type: Any
    category: str
    party: str
    amount: Decimal
    fund: str
    account: Any
    notes: str
    date: Any
    transfer_id: Any


def _value(item: Any) -> Any:
    """Return the plain value of a ``StrEnum`` member or the raw value."""
    return getattr(item, "value", item)


def account_type(name: str) -> AccountType:
    """Classify an account name for the chart of accounts."""
    if name in ACCOUNTS:
        return AccountType.ASSET
    if name in INCOME_CATEGORIES:
        return AccountType.INCOME
    if name in EXPENSE_CATEGORIES:
        return AccountType.EXPENSE
    return AccountType.OTHER


def is_transfer(transaction: TransactionLike) -> bool:
    return transaction.category == TRANSFER_CATEGORY


def journal_lines(transactions: Iterable[TransactionLike]) -> list[JournalLine]:
    """Expand transactions into balanced debit/credit journal lines.

    * income  → debit payment account, credit income category
    * expense → debit expense category, credit payment account
    * transfer → debit destination account, credit source account
    """
    lines: list[JournalLine] = []
    for tx in transactions:
        amount = Decimal(tx.amount).quantize(Decimal("0.01"))
        if amount <= ZERO:
            continue
        account = _value(tx.account)
        description = f"{_value(tx.type).title()} · {tx.category}"

        if is_transfer(tx):
            if _value(tx.type) == "expense":  # money leaving the source account
                lines.append(
                    JournalLine(
                        date=tx.date,
                        account=account,
                        debit=ZERO,
                        credit=amount,
                        transaction_id=tx.id,
                        description=description,
                    )
                )
            else:  # money arriving in the destination account
                lines.append(
                    JournalLine(
                        date=tx.date,
                        account=account,
                        debit=amount,
                        credit=ZERO,
                        transaction_id=tx.id,
                        description=description,
                    )
                )
            continue

        if _value(tx.type) == "income":
            lines.append(
                JournalLine(
                    date=tx.date,
                    account=account,
                    debit=amount,
                    credit=ZERO,
                    transaction_id=tx.id,
                    description=description,
                )
            )
            lines.append(
                JournalLine(
                    date=tx.date,
                    account=tx.category,
                    debit=ZERO,
                    credit=amount,
                    transaction_id=tx.id,
                    description=description,
                )
            )
        else:
            lines.append(
                JournalLine(
                    date=tx.date,
                    account=tx.category,
                    debit=amount,
                    credit=ZERO,
                    transaction_id=tx.id,
                    description=description,
                )
            )
            lines.append(
                JournalLine(
                    date=tx.date,
                    account=account,
                    debit=ZERO,
                    credit=amount,
                    transaction_id=tx.id,
                    description=description,
                )
            )
    return lines


def _sum(values: Iterable[Decimal]) -> Decimal:
    return sum(values, ZERO).quantize(Decimal("0.01"))


#: Public alias so route modules never reach for a private helper.
sum_money = _sum


def account_balance(lines: Sequence[JournalLine], account: str) -> Decimal:
    """Debit minus credit balance of a single account."""
    return _sum(line.debit - line.credit for line in lines if line.account == account)


def chart_of_accounts(transactions: Iterable[TransactionLike]) -> list[AccountSummary]:
    lines = journal_lines(transactions)
    summaries: list[AccountSummary] = []
    for name in (*ACCOUNTS, *INCOME_CATEGORIES, *EXPENSE_CATEGORIES):
        account_lines = [line for line in lines if line.account == name]
        if not account_lines:
            continue
        debit = _sum(line.debit for line in account_lines)
        credit = _sum(line.credit for line in account_lines)
        summaries.append(
            AccountSummary(
                account=name,
                account_type=account_type(name).value,
                debit=debit,
                credit=credit,
                balance=(debit - credit).quantize(Decimal("0.01")),
            )
        )
    return summaries


def trial_balance(
    transactions: Iterable[TransactionLike],
) -> tuple[list[AccountSummary], Decimal, Decimal]:
    accounts = chart_of_accounts(transactions)
    total_debits = _sum(a.debit for a in accounts)
    total_credits = _sum(a.credit for a in accounts)
    return accounts, total_debits, total_credits


def operating_transactions(transactions: Iterable[TransactionLike]) -> list[TransactionLike]:
    """Transactions that affect the income statement (transfers excluded)."""
    return [tx for tx in transactions if not is_transfer(tx)]


def income_total(transactions: Iterable[TransactionLike]) -> Decimal:
    return _sum(
        tx.amount for tx in transactions if _value(tx.type) == "income" and not is_transfer(tx)
    )


def expense_total(transactions: Iterable[TransactionLike]) -> Decimal:
    return _sum(
        tx.amount for tx in transactions if _value(tx.type) == "expense" and not is_transfer(tx)
    )


def totals_by_category(
    transactions: Iterable[TransactionLike],
    categories: Sequence[str],
    tx_type: str,
) -> list[CategoryTotal]:
    rows = [tx for tx in transactions if _value(tx.type) == tx_type]
    return [
        CategoryTotal(
            category=category, total=_sum(tx.amount for tx in rows if tx.category == category)
        )
        for category in categories
    ]


def balance_by_account(transactions: Iterable[TransactionLike]) -> list[NamedTotal]:
    rows = list(transactions)
    return [
        NamedTotal(
            key=account,
            total=_sum(
                tx.amount
                for tx in rows
                if _value(tx.account) == account and _value(tx.type) == "income"
            )
            - _sum(
                tx.amount
                for tx in rows
                if _value(tx.account) == account and _value(tx.type) == "expense"
            ),
        )
        for account in ACCOUNTS
    ]


def net_by_fund(transactions: Iterable[TransactionLike]) -> list[NamedTotal]:
    rows = list(transactions)
    return [
        NamedTotal(
            key=fund,
            total=_sum(tx.amount for tx in rows if tx.fund == fund and _value(tx.type) == "income")
            - _sum(tx.amount for tx in rows if tx.fund == fund and _value(tx.type) == "expense"),
        )
        for fund in FUNDS
    ]


def fund_position(transactions: Iterable[TransactionLike], fund: str) -> tuple[Decimal, Decimal]:
    """Return ``(received, spent)`` for a single fund."""
    rows = [tx for tx in transactions if tx.fund == fund]
    received = _sum(tx.amount for tx in rows if _value(tx.type) == "income")
    spent = _sum(tx.amount for tx in rows if _value(tx.type) == "expense")
    return received, spent


def tithes_on_date(transactions: Iterable[TransactionLike], day: Any) -> Decimal:
    """Total of the Tithes collected on ``day``.

    Only real income counts: a transfer arriving in an account on that date is
    not a tithe, so it is excluded exactly like it is everywhere else.
    """
    return _sum(
        tx.amount
        for tx in transactions
        if _value(tx.type) == "income"
        and tx.category == IncomeCategory.TITHES.value
        and tx.date == day
    )


def tithes_entry_count(transactions: Iterable[TransactionLike], day: Any) -> int:
    """How many separate tithe entries were recorded on ``day``."""
    return sum(
        1
        for tx in transactions
        if _value(tx.type) == "income"
        and tx.category == IncomeCategory.TITHES.value
        and tx.date == day
    )


def deduction_amount(base: Decimal, pct: Decimal) -> Decimal:
    """The amount a percentage takes off ``base``, rounded to the cent.

    Rounding happens once, on the result, so a 10% deduction on 1,000.01 is
    exactly 100.00 rather than 100.001 truncated by a column type.
    """
    base = Decimal(base).quantize(Decimal("0.01"))
    pct = Decimal(pct)
    return (base * pct / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class TransferPair:
    """A matched pair of transfer rows."""

    transfer_id: str
    date: Any
    source_account: str
    destination_account: str
    amount: Decimal
    notes: str


def pair_transfers(transactions: Iterable[TransactionLike]) -> list[TransferPair]:
    """Join transfer rows into readable Cash → Bank style pairs."""
    outgoing: dict[str, list[TransactionLike]] = {}
    incoming: dict[str, list[TransactionLike]] = {}
    for tx in transactions:
        if not is_transfer(tx) or tx.transfer_id is None:
            continue
        key = str(tx.transfer_id)
        if _value(tx.type) == "expense":
            outgoing.setdefault(key, []).append(tx)
        else:
            incoming.setdefault(key, []).append(tx)

    pairs: list[TransferPair] = []
    for key, source_rows in outgoing.items():
        source = source_rows[0]
        destination = next(
            (
                row
                for row in incoming.get(key, [])
                if row.date == source.date and row.amount == source.amount
            ),
            incoming.get(key, [None])[0],
        )
        pairs.append(
            TransferPair(
                transfer_id=key,
                date=source.date,
                source_account=_value(source.account),
                destination_account=_value(destination.account) if destination else "",
                amount=Decimal(source.amount).quantize(Decimal("0.01")),
                notes=source.notes or "",
            )
        )
    return sorted(pairs, key=lambda pair: (pair.date, pair.source_account))
