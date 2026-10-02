"""Unit tests for the double-entry accounting engine (no database required)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

import pytest
from app.enums import FUNDS, TRANSFER_CATEGORY, Account, Fund
from app.services import accounting


class TxType(StrEnum):
    INCOME = "income"
    EXPENSE = "expense"


@dataclass
class FakeTransaction:
    """Minimal stand-in for the ORM row."""

    id: uuid.UUID
    type: TxType
    category: str
    amount: Decimal
    account: str
    fund: str = ""
    party: str = ""
    notes: str = ""
    date: date = date(2026, 1, 15)
    transfer_id: uuid.UUID | None = None


def make(**overrides: Any) -> FakeTransaction:
    defaults = {
        "id": uuid.uuid4(),
        "type": TxType.INCOME,
        "category": "Tithes",
        "amount": Decimal("1000.00"),
        "account": "Cash",
    }
    return FakeTransaction(**{**defaults, **overrides})


class TestJournalLines:
    def test_income_debits_account_and_credits_category(self) -> None:
        lines = accounting.journal_lines([make()])
        assert len(lines) == 2
        assert (lines[0].account, lines[0].debit, lines[0].credit) == (
            "Cash",
            Decimal("1000.00"),
            Decimal("0.00"),
        )
        assert (lines[1].account, lines[1].debit, lines[1].credit) == (
            "Tithes",
            Decimal("0.00"),
            Decimal("1000.00"),
        )

    def test_expense_debits_category_and_credits_account(self) -> None:
        lines = accounting.journal_lines([make(type=TxType.EXPENSE, category="Expenses")])
        assert (lines[0].account, lines[0].debit) == ("Expenses", Decimal("1000.00"))
        assert (lines[1].account, lines[1].credit) == ("Cash", Decimal("1000.00"))

    def test_transfer_only_touches_payment_accounts(self) -> None:
        transfer_id = uuid.uuid4()
        lines = accounting.journal_lines(
            [
                make(type=TxType.EXPENSE, category=TRANSFER_CATEGORY, transfer_id=transfer_id),
                make(type=TxType.INCOME, category=TRANSFER_CATEGORY, transfer_id=transfer_id),
            ]
        )
        assert {line.account for line in lines} == {"Cash"}
        # 1,000 leaves Cash (credit) and 1,000 arrives in Cash (debit).
        assert (
            sum(line.debit for line in lines)
            == sum(line.credit for line in lines)
            == Decimal("1000.00")
        )

    def test_zero_and_negative_amounts_are_ignored(self) -> None:
        assert accounting.journal_lines([make(amount=Decimal("0"))]) == []
        assert accounting.journal_lines([make(amount=Decimal("-5"))]) == []

    def test_debits_equal_credits_for_any_mix(self) -> None:
        transactions = [
            make(amount=Decimal("1500.50"), account="Bank"),
            make(
                type=TxType.EXPENSE,
                category="Petty Cash",
                amount=Decimal("200.25"),
                account="M-PESA",
            ),
            make(category="Offerings", amount=Decimal("75.25"), account="Cash"),
        ]
        lines = accounting.journal_lines(transactions)
        assert accounting.sum_money(line.debit for line in lines) == accounting.sum_money(
            line.credit for line in lines
        )


class TestBalances:
    def test_trial_balance_is_balanced(self) -> None:
        transactions = [
            make(amount=Decimal("10000"), account="Bank"),
            make(type=TxType.EXPENSE, category="Suppliers", amount=Decimal("2500"), account="Bank"),
        ]
        accounts, total_debits, total_credits = accounting.trial_balance(transactions)
        # debits: Bank 10,000 + Suppliers 2,500 | credits: Tithes 10,000 + Bank 2,500
        assert total_debits == total_credits == Decimal("12500.00")
        assert [a.account for a in accounts] == ["Bank", "Tithes", "Suppliers"]

    def test_account_balance_reflects_income_minus_expense(self) -> None:
        transactions = [
            make(amount=Decimal("10000"), account="Bank"),
            make(type=TxType.EXPENSE, category="Expenses", amount=Decimal("2500"), account="Bank"),
        ]
        lines = accounting.journal_lines(transactions)
        assert accounting.account_balance(lines, "Bank") == Decimal("7500.00")
        assert accounting.account_balance(lines, "Tithes") == Decimal("-10000.00")

    def test_balance_by_account_covers_every_account(self) -> None:
        balances = accounting.balance_by_account([make(amount=Decimal("300"), account="M-PESA")])
        assert [b.key for b in balances] == ["Cash", "Bank", "M-PESA"]
        assert next(b for b in balances if b.key == "M-PESA").total == Decimal("300.00")

    def test_net_by_fund_covers_every_fund(self) -> None:
        funds = accounting.net_by_fund(
            [
                make(amount=Decimal("500"), fund="Building"),
                make(
                    type=TxType.EXPENSE, category="Expenses", amount=Decimal("200"), fund="Building"
                ),
            ]
        )
        # Every fund appears, including the ones with no activity, so the
        # assertion follows the enum rather than a hard-coded count.
        assert [f.key for f in funds] == list(FUNDS)
        assert next(f for f in funds if f.key == "Building").total == Decimal("300.00")

    def test_transfers_do_not_affect_income_or_expenses(self) -> None:
        transfer_id = uuid.uuid4()
        transactions = [
            make(
                type=TxType.EXPENSE,
                category=TRANSFER_CATEGORY,
                transfer_id=transfer_id,
                amount=Decimal("5000"),
            ),
            make(
                type=TxType.INCOME,
                category=TRANSFER_CATEGORY,
                transfer_id=transfer_id,
                amount=Decimal("5000"),
            ),
        ]
        assert accounting.income_total(transactions) == Decimal("0.00")
        assert accounting.expense_total(transactions) == Decimal("0.00")
        assert len(accounting.operating_transactions(transactions)) == 0


class TestTransferPairing:
    def test_outgoing_row_is_matched_with_its_destination(self) -> None:
        transfer_id = uuid.uuid4()
        transactions = [
            make(
                id=uuid.uuid4(),
                type=TxType.EXPENSE,
                category=TRANSFER_CATEGORY,
                account="Bank",
                amount=Decimal("15000"),
                transfer_id=transfer_id,
                notes="Cash withdrawal",
            ),
            make(
                id=uuid.uuid4(),
                type=TxType.INCOME,
                category=TRANSFER_CATEGORY,
                account="Cash",
                amount=Decimal("15000"),
                transfer_id=transfer_id,
            ),
        ]
        pairs = accounting.pair_transfers(transactions)
        assert len(pairs) == 1
        assert pairs[0].source_account == "Bank"
        assert pairs[0].destination_account == "Cash"
        assert pairs[0].amount == Decimal("15000.00")
        assert pairs[0].notes == "Cash withdrawal"

    def test_multiple_transfers_are_paired_independently(self) -> None:
        transactions = []
        for source, destination in (("Bank", "Cash"), ("Cash", "M-PESA"), ("M-PESA", "Bank")):
            transfer_id = uuid.uuid4()
            transactions.append(
                make(
                    type=TxType.EXPENSE,
                    category=TRANSFER_CATEGORY,
                    account=source,
                    transfer_id=transfer_id,
                )
            )
            transactions.append(
                make(
                    type=TxType.INCOME,
                    category=TRANSFER_CATEGORY,
                    account=destination,
                    transfer_id=transfer_id,
                )
            )
        pairs = accounting.pair_transfers(transactions)
        assert len(pairs) == 3
        assert {(p.source_account, p.destination_account) for p in pairs} == {
            ("Bank", "Cash"),
            ("Cash", "M-PESA"),
            ("M-PESA", "Bank"),
        }


class TestTithesOnDate:
    def test_only_tithes_income_on_that_day_count(self) -> None:
        sunday = date(2026, 3, 8)
        saturday = date(2026, 3, 7)
        transactions = [
            make(category="Tithes", amount=Decimal("3000"), date=sunday),
            make(category="Tithes", amount=Decimal("1000"), date=sunday),
            make(category="Offerings", amount=Decimal("500"), date=sunday),
            make(category="Tithes", amount=Decimal("9999"), date=saturday),
            make(
                type=TxType.EXPENSE,
                category="Expenses",
                amount=Decimal("500"),
                date=sunday,
            ),
        ]
        assert accounting.tithes_on_date(transactions, sunday) == Decimal("4000.00")
        assert accounting.tithes_entry_count(transactions, sunday) == 2

    def test_a_transfer_arriving_that_day_is_not_a_tithe(self) -> None:
        sunday = date(2026, 3, 8)
        transfer_id = uuid.uuid4()
        transactions = [
            make(
                type=TxType.INCOME,
                category=TRANSFER_CATEGORY,
                amount=Decimal("5000"),
                date=sunday,
                transfer_id=transfer_id,
            )
        ]
        assert accounting.tithes_on_date(transactions, sunday) == Decimal("0.00")

    def test_a_day_with_no_tithes_totals_zero(self) -> None:
        assert accounting.tithes_on_date([], date(2026, 3, 8)) == Decimal("0.00")


class TestDeductionAmount:
    @pytest.mark.parametrize(
        ("base", "pct", "expected"),
        [
            ("4000.00", "10", "400.00"),
            ("8333.33", "7.5", "625.00"),
            ("1000.01", "10", "100.00"),
            ("5000.00", "0.1", "5.00"),
            ("10.00", "0.1", "0.01"),
            ("0", "50", "0.00"),
            ("2500", "100", "2500.00"),
        ],
    )
    def test_rounds_once_on_the_result(self, base: str, pct: str, expected: str) -> None:
        assert accounting.deduction_amount(Decimal(base), Decimal(pct)) == Decimal(expected)

    def test_repeated_deductions_on_the_same_day_each_use_the_full_base(self) -> None:
        # The base is the Sunday's tithe total, not a running total, so two
        # 10% deductions on a 1,000 Sunday are 100 + 100, not 100 + 90.
        base = Decimal("1000.00")
        first = accounting.deduction_amount(base, Decimal("10"))
        second = accounting.deduction_amount(base, Decimal("10"))
        assert first == second == Decimal("100.00")

    def test_shares_add_back_up_to_the_single_figure_total(self) -> None:
        tithes = {Account.CASH: Decimal("333.33"), Account.BANK: Decimal("666.67")}
        shares = accounting.deduction_shares(tithes, Decimal("10"))
        total = accounting.deduction_amount(Decimal("1000.00"), Decimal("10"))
        # Rounding each share alone would land a cent away from the total.
        assert sum(share.amount for share in shares) == total
        assert {share.account: share.amount for share in shares} == {
            Account.CASH: Decimal("33.33"),
            Account.BANK: Decimal("66.67"),
        }

    def test_a_single_account_share_needs_no_rounding_correction(self) -> None:
        shares = accounting.deduction_shares({Account.BANK: Decimal("4000.00")}, Decimal("10"))
        assert [share.amount for share in shares] == [Decimal("400.00")]

    def test_accounts_with_no_tithes_are_left_out(self) -> None:
        shares = accounting.deduction_shares(
            {Account.CASH: Decimal("0.00"), Account.BANK: Decimal("1000.00")}, Decimal("10")
        )
        assert [share.account for share in shares] == [Account.BANK]

    def test_a_share_that_rounds_to_nothing_is_not_posted(self) -> None:
        shares = accounting.deduction_shares(
            {Account.CASH: Decimal("1.00"), Account.BANK: Decimal("1000.00")}, Decimal("0.1")
        )
        # 0.1% of 1.00 is 0.001, which is not an entry; the Bank share stands.
        assert [share.account for share in shares] == [Account.BANK]


class TestAccountType:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            (Account.CASH, "Asset"),
            ("Tithes", "Income"),
            ("Petty Cash", "Expense"),
            ("Percentage Deduction", "Expense"),
            ("Something else", "Other"),
        ],
    )
    def test_classification(self, name: str, expected: str) -> None:
        assert accounting.account_type(name).value == expected


class TestEnumInterop:
    def test_real_enum_members_work_as_accounts(self) -> None:
        transactions = [make(account=Account.M_PESA, amount=Decimal("1200"))]
        assert accounting.balance_by_account(transactions)[2].total == Decimal("1200.00")

    def test_fund_position_returns_received_and_spent(self) -> None:
        received, spent = accounting.fund_position(
            [
                make(amount=Decimal("900"), fund=Fund.YOUTH.value),
                make(
                    type=TxType.EXPENSE,
                    category="Expenses",
                    amount=Decimal("100"),
                    fund=Fund.YOUTH.value,
                ),
            ],
            Fund.YOUTH.value,
        )
        assert received == Decimal("900.00")
        assert spent == Decimal("100.00")
