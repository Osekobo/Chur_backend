"""Add the audit_log table.

An append-only record of who changed what, when and from where. ``actor_email`` is
denormalised so an entry still names a person after their account is removed, and
``changes`` holds a JSONB before/after map for edits.

There is intentionally no foreign key on ``actor_id`` with ON DELETE CASCADE:
removing an account must not silently remove the history of what it did.

Revision ID: 0003_audit_log
Revises: 0002_pct_deduction
Create Date: 2026-10-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_audit_log"
down_revision: str | None = "0002_pct_deduction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The lifecycle object: used to CREATE TYPE in upgrade() and DROP it in
#: downgrade().
audit_action = postgresql.ENUM(
    "create",
    "update",
    "delete",
    "login",
    "login_failed",
    "logout",
    "role_change",
    "password_reset",
    "account_deactivated",
    "account_reactivated",
    "approve",
    "reject",
    name="audit_action",
)
audit_entity = postgresql.ENUM(
    "transaction", "person", "user", "auth", "approval", name="audit_entity"
)

#: The column types carry ``create_type=False`` because the types are created
#: explicitly above. Without it SQLAlchemy emits a second CREATE TYPE when
#: create_table() runs and the migration dies with DuplicateObject.
audit_action_col = postgresql.ENUM(
    *audit_action.enums, name="audit_action", create_type=False
)
audit_entity_col = postgresql.ENUM(
    *audit_entity.enums, name="audit_entity", create_type=False
)


def upgrade() -> None:
    audit_action.create(op.get_bind(), checkfirst=True)
    audit_entity.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_email", sa.String(length=320), server_default="", nullable=False),
        sa.Column("action", audit_action_col, nullable=False),
        sa.Column("entity_type", audit_entity_col, nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("changes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ip_address", sa.String(length=45), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_audit_log_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    # The list screen is always "newest first", and the actor/entity filters are
    # hit on every page, so each gets its own index.
    op.create_index(
        "ix_audit_log_created_at_desc",
        "audit_log",
        [sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index("ix_audit_log_actor", "audit_log", ["actor_id"], unique=False)
    op.create_index(
        "ix_audit_log_entity", "audit_log", ["entity_type", "entity_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_index("ix_audit_log_actor", table_name="audit_log")
    op.drop_index("ix_audit_log_created_at_desc", table_name="audit_log")
    op.drop_table("audit_log")
    audit_entity.drop(op.get_bind(), checkfirst=True)
    audit_action.drop(op.get_bind(), checkfirst=True)
