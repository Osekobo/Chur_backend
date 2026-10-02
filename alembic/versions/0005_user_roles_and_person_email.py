"""Add user roles, the person email and the Guest directory role.

Three changes that belong together because they are one story: the church office
is split into three roles, and to make that work the directory needs somewhere to
keep an email address and one more kind of person.

* ``users.role`` replaces ``users.is_superuser``. The old flag could only say
  "may manage accounts" or "may not", which left no way to describe an accountant
  or a secretary. Existing superusers become ``admin``; everyone else becomes
  ``accountant``, the least privileged role that can still do the money work the
  app was previously open to. The column is dropped afterwards - ``role`` is the
  only source of truth, so keeping both would let them disagree.
* ``people.email`` is optional for members and guests and mandatory for suppliers,
  employees and users, which is enforced in the schema rather than the database.
* ``person_role`` gains ``Guest``: someone who gives regularly without being on the
  membership roll. Without a separate role they had to be filed as Members, which
  inflated the membership count.

Revision ID: 0005_user_roles_and_person_email
Revises: 0004_approval_requests
Create Date: 2026-10-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_user_roles_and_person_email"
down_revision: str | None = "0004_approval_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

user_role = postgresql.ENUM("accountant", "secretary", "admin", name="user_role")


def upgrade() -> None:
    bind = op.get_bind()
    user_role.create(bind, checkfirst=True)

    # Added as nullable so the existing rows can be classified before the
    # NOT NULL default lands; one pass over the table beats a table rewrite.
    op.add_column("users", sa.Column("role", postgresql.ENUM(name="user_role", create_type=False)))
    op.execute("UPDATE users SET role = 'admin' WHERE is_superuser")
    op.execute("UPDATE users SET role = 'accountant' WHERE role IS NULL")
    op.alter_column(
        "users",
        "role",
        nullable=False,
        server_default=sa.text("'accountant'"),
    )
    op.drop_column("users", "is_superuser")

    op.add_column(
        "people",
        sa.Column("email", sa.String(length=320), nullable=False, server_default=""),
    )
    # The address is now searchable from the directory's search box, which is a
    # case-insensitive substring match.
    op.create_index("ix_people_lower_email", "people", [sa.text("lower(email)")], unique=False)

    op.execute("ALTER TYPE person_role ADD VALUE IF NOT EXISTS 'Guest'")


def downgrade() -> None:
    op.drop_index("ix_people_lower_email", table_name="people")
    op.drop_column("people", "email")

    op.add_column("users", sa.Column("is_superuser", sa.Boolean(), nullable=True))
    op.execute("UPDATE users SET is_superuser = true WHERE role = 'admin'")
    op.execute("UPDATE users SET is_superuser = false WHERE role IS DISTINCT FROM 'admin'")
    op.alter_column("users", "is_superuser", nullable=False, server_default=sa.text("false"))
    op.drop_column("users", "role")
    user_role.drop(op.get_bind(), checkfirst=True)

    # PostgreSQL cannot remove an enum label, so 'Guest' stays in the type. Leaving
    # it is harmless: nothing reads it once the ORM stops offering it.