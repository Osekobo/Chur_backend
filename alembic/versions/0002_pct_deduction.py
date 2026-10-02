"""Add the Tithe % Deduction columns to the transaction ledger.

Adds ``pct`` and ``base_total`` so a "Percentage Deduction" entry remembers the
percentage that was applied and the Sunday tithe total it came from, plus a
partial index that makes the deduction history cheap to read.

Revision ID: 0002_pct_deduction
Revises: 0001_initial
Create Date: 2026-09-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_pct_deduction"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("pct", sa.Numeric(precision=6, scale=2), nullable=True),
    )
    op.add_column(
        "transactions",
        sa.Column("base_total", sa.Numeric(precision=14, scale=2), nullable=True),
    )
    # A percentage and its base only make sense together.
    op.create_check_constraint(
        op.f("ck_transactions_pct_base_total_pair"),
        "transactions",
        "(pct IS NULL) = (base_total IS NULL)",
    )
    # ...and only on a real deduction.
    op.create_check_constraint(
        op.f("ck_transactions_pct_on_deduction"),
        "transactions",
        "(pct IS NULL) OR category = 'Percentage Deduction'",
    )
    op.create_index(
        "ix_transactions_pct_deduction",
        "transactions",
        ["date"],
        unique=False,
        postgresql_where=sa.text("category = 'Percentage Deduction'"),
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_pct_deduction", table_name="transactions")
    op.drop_constraint(op.f("ck_transactions_pct_on_deduction"), "transactions", type_="check")
    op.drop_constraint(op.f("ck_transactions_pct_base_total_pair"), "transactions", type_="check")
    op.drop_column("transactions", "base_total")
    op.drop_column("transactions", "pct")
