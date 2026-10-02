"""Refuse the same person twice, and the same collection saved twice.

Two ways one record became two:

* the directory accepted a name it already held, so a giver existed as two rows and
  every total was split between them;
* an entry could be written twice - a double-click, a retry after a dropped
  connection, or a form submitted again from the stale list it had not yet
  refreshed - and the church banked one collection twice.

Both are now the database's job, not the form's:

* ``people`` gains a unique index on (role, lower(name)). Within a role a name
  identifies a person, so "Angela Ochieng" can be added once. The API answers 409
  with the person's name when it would collide, so the secretary is told who is
  already there instead of seeing a generic conflict.
* ``transactions`` gains ``client_request_id``, the token the form generates for an
  entry, with a unique index. A repeated submission carrying the same token returns
  the entry that already exists instead of writing a second one. Rows written by
  anything that is not a form keep a null token, and a unique index allows any
  number of nulls, so transfers and deductions are untouched.

Existing duplicate names are collapsed before the index goes on, because an index
cannot be created over them. The earliest row survives - creation time, then id -
its ledger history is repointed to it, and details the survivor is missing are
filled in from the copies, so nothing that was given is lost. That merge is not
undone on downgrade: the copies it removed were not distinguishable from the row
kept, and putting a deleted name back would be guesswork.

Duplicate money-in entries are deliberately *not* merged: two identical collections
may well be two real ones, and deleting money is not this migration's call. The API
refuses a second identical entry and says so; where old duplicates exist, the
secretary sees them in the list and deletes the one that is wrong, as before.

Revision ID: 0007_no_duplicates
Revises: 0006_transaction_person
Create Date: 2026-10-02

"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_no_duplicates"
down_revision: str | None = "0006_transaction_person"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")


def _merge_duplicate_people() -> None:
    """Collapse same-role, same-name rows onto the earliest of each group."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, role, name, phone, email, category, notes"
            " FROM people ORDER BY created_at, id"
        )
    ).mappings().all()

    groups: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in rows:
        key = (str(row["role"]), str(row["name"]).lower())
        groups.setdefault(key, []).append(dict(row))

    merged = 0
    for (role, name), copies in groups.items():
        if len(copies) < 2:
            continue
        keeper = copies[0]
        for copy_ in copies[1:]:
            # Point the ledger at the row that survives, then fill in anything the
            # survivor is missing before the copy goes: a duplicate added without a
            # phone would otherwise lose the phone number the original had.
            bind.execute(
                sa.text("UPDATE transactions SET person_id = :keeper WHERE person_id = :copy"),
                {"keeper": keeper["id"], "copy": copy_["id"]},
            )
            bind.execute(
                sa.text(
                    "UPDATE people SET"
                    " phone = CASE WHEN phone = '' THEN :phone ELSE phone END,"
                    " email = CASE WHEN email = '' THEN :email ELSE email END,"
                    " category = CASE WHEN category = '' THEN :category ELSE category END,"
                    " notes = CASE WHEN notes = '' THEN :notes ELSE notes END"
                    " WHERE id = :keeper"
                ),
                {
                    "keeper": keeper["id"],
                    "phone": copy_["phone"],
                    "email": copy_["email"],
                    "category": copy_["category"],
                    "notes": copy_["notes"],
                },
            )
            bind.execute(sa.text("DELETE FROM people WHERE id = :copy"), {"copy": copy_["id"]})
            merged += 1
        log.info("merged duplicate %s %r onto %s", role, name, keeper["id"])

    if merged:
        log.info("collapsed %s duplicate directory row(s)", merged)


def upgrade() -> None:
    _merge_duplicate_people()
    op.create_index(
        "uq_people_role_lower_name",
        "people",
        ["role", sa.text("lower(name)")],
        unique=True,
    )

    op.add_column(
        "transactions",
        sa.Column("client_request_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "uq_transactions_client_request_id",
        "transactions",
        ["client_request_id"],
        unique=True,
    )


def downgrade() -> None:
    # The merged directory rows stay merged: see the module docstring.
    op.drop_index("uq_transactions_client_request_id", table_name="transactions")
    op.drop_column("transactions", "client_request_id")
    op.drop_index("uq_people_role_lower_name", table_name="people")