"""Add the approval_requests table.

A money-out request waits here as ``pending`` until an administrator approves it,
at which point a matching ``transactions`` row is written and linked through
``transaction_id``. Nothing else in the app reads this table, so a pending request
cannot move a balance or a report.

``status`` is indexed because the Approvals screen sorts pending-first and the
sidebar badge counts by status.

Revision ID: 0004_approval_requests
Revises: 0003_audit_log
Create Date: 2026-10-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_approval_requests"
down_revision: str | None = "0003_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

approval_status = postgresql.ENUM(
    "pending", "approved", "rejected", name="approval_status"
)
#: Created explicitly in upgrade(), so the column's copy must not re-emit CREATE
#: TYPE when create_table() runs.
approval_status_col = postgresql.ENUM(
    *approval_status.enums, name="approval_status", create_type=False
)
#: ``account`` was created by 0001_initial. Referencing it by name only - no
#: ``create_type`` and no empty value list, which would emit an invalid type.
account_col = postgresql.ENUM(name="account", create_type=False)


def upgrade() -> None:
    approval_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "approval_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", approval_status_col, nullable=False),
        sa.Column("category", sa.String(length=60), nullable=False),
        sa.Column("party", sa.String(length=200), server_default="", nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("fund", sa.String(length=60), server_default="", nullable=False),
        sa.Column("account", account_col, nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("requested_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.String(length=500), server_default="", nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"],
            ["users.id"],
            name=op.f("fk_approval_requests_requested_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_id"],
            ["users.id"],
            name=op.f("fk_approval_requests_decided_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_approval_requests_transaction_id_transactions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_requests")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_approval_requests_amount_positive")),
    )
    op.create_index(
        "ix_approval_requests_status", "approval_requests", ["status"], unique=False
    )
    op.create_index(
        "ix_approval_requests_created_at_desc",
        "approval_requests",
        [sa.text("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_approval_requests_created_at_desc", table_name="approval_requests")
    op.drop_index("ix_approval_requests_status", table_name="approval_requests")
    op.drop_table("approval_requests")
    approval_status.drop(op.get_bind(), checkfirst=True)
