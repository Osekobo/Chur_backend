"""Initial schema: users, sessions, people and the transaction ledger.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Native enum types are created explicitly (create_type=False stops
# create_table from emitting a second CREATE TYPE) so `downgrade` can drop them.
person_role = postgresql.ENUM(
    "Member", "Supplier", "Employee", "User", name="person_role", create_type=False
)
transaction_type = postgresql.ENUM("income", "expense", name="transaction_type", create_type=False)
account = postgresql.ENUM("Cash", "Bank", "M-PESA", name="account", create_type=False)

ALL_ENUMS = (person_role, transaction_type, account)


def upgrade() -> None:
    for enum_type in ALL_ENUMS:
        enum_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_superuser", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_refresh_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
    )
    op.create_index(
        op.f("ix_refresh_tokens_token_hash"), "refresh_tokens", ["token_hash"], unique=True
    )
    op.create_index(op.f("ix_refresh_tokens_user_id"), "refresh_tokens", ["user_id"], unique=False)

    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_password_reset_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_password_reset_tokens")),
    )
    op.create_index(
        op.f("ix_password_reset_tokens_token_hash"),
        "password_reset_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_password_reset_tokens_user_id"), "password_reset_tokens", ["user_id"], unique=False
    )

    op.create_table(
        "people",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("role", person_role, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("phone", sa.String(length=40), server_default="", nullable=False),
        sa.Column("category", sa.String(length=60), server_default="", nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_people")),
    )
    op.create_index(
        "ix_people_lower_name", "people", [sa.literal_column("lower(name)")], unique=False
    )
    op.create_index("ix_people_role", "people", ["role"], unique=False)

    op.create_table(
        "transactions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("type", transaction_type, nullable=False),
        sa.Column("category", sa.String(length=60), nullable=False),
        sa.Column("party", sa.String(length=200), server_default="", nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("fund", sa.String(length=60), server_default="", nullable=False),
        sa.Column("account", account, nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("transfer_id", sa.UUID(), nullable=True),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_transactions_amount_positive")),
        # Transfer rows must always carry the link to their opposite leg.
        sa.CheckConstraint(
            "(category = 'Transfer') = (transfer_id IS NOT NULL)",
            name=op.f("ck_transactions_transfer_category_matches_link"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_transactions_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transactions")),
    )
    op.create_index("ix_transactions_account", "transactions", ["account"], unique=False)
    op.create_index("ix_transactions_category", "transactions", ["category"], unique=False)
    op.create_index(
        "ix_transactions_date_desc", "transactions", [sa.literal_column("date DESC")], unique=False
    )
    op.create_index("ix_transactions_fund", "transactions", ["fund"], unique=False)
    op.create_index(
        "ix_transactions_lower_party",
        "transactions",
        [sa.literal_column("lower(party)")],
        unique=False,
    )
    op.create_index("ix_transactions_transfer_id", "transactions", ["transfer_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_transactions_transfer_id", table_name="transactions")
    op.drop_index("ix_transactions_lower_party", table_name="transactions")
    op.drop_index("ix_transactions_fund", table_name="transactions")
    op.drop_index("ix_transactions_date_desc", table_name="transactions")
    op.drop_index("ix_transactions_category", table_name="transactions")
    op.drop_index("ix_transactions_account", table_name="transactions")
    op.drop_table("transactions")

    op.drop_index(op.f("ix_people_role"), table_name="people")
    op.drop_index("ix_people_lower_name", table_name="people")
    op.drop_table("people")

    op.drop_index(op.f("ix_password_reset_tokens_user_id"), table_name="password_reset_tokens")
    op.drop_index(op.f("ix_password_reset_tokens_token_hash"), table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")

    op.drop_index(op.f("ix_refresh_tokens_user_id"), table_name="refresh_tokens")
    op.drop_index(op.f("ix_refresh_tokens_token_hash"), table_name="refresh_tokens")
    op.drop_table("refresh_tokens")

    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")

    for enum_type in reversed(ALL_ENUMS):
        enum_type.drop(op.get_bind(), checkfirst=True)
