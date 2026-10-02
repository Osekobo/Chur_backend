"""Link money in to the person it came from.

Money in used to be a name typed into a box. That made "who gave what" unanswerable
without matching on spelling: "A. Ochieng" and "Angela Ochieng" were two people,
and a later search could not tell which rows belonged together. So the giver is now
the person, and ``transactions.person_id`` records who they are.

``party`` is kept and filled in from the directory row at write time. Nothing is
lost by that: the reports group income by name exactly as before, and the list
search still matches a name - the copy is a faithful record of the name the money
was given under, even if the directory entry is renamed or deleted later.

The column is nullable, and that is not a loophole:

* a transfer's income leg is the church's own money arriving in another account,
  so it has no giver and is written by the transfer endpoint, not by a person;
* rows recorded before this migration have no link, and cannot be invented after
  the fact without guessing which of two names in the ledger was which person.

New money in is required to name a person, by ``TransactionCreate`` - the same
schema the approval endpoint writes through.

Revision ID: 0006_transaction_person
Revises: 0005_user_roles_and_person_email
Create Date: 2026-10-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_transaction_person"
down_revision: str | None = "0005_user_roles_and_person_email"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # SET NULL, not CASCADE: removing someone from the directory must not erase
    # the history of what they gave. The link goes and party keeps the name.
    op.create_foreign_key(
        "fk_transactions_person_id",
        "transactions",
        "people",
        ["person_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_transactions_person_id", "transactions", ["person_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_transactions_person_id", table_name="transactions")
    op.drop_constraint("fk_transactions_person_id", "transactions", type_="foreignkey")
    op.drop_column("transactions", "person_id")