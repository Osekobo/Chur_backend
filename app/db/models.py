"""SQLAlchemy ORM models."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, enum_column, uuid_pk
from app.enums import (
    Account,
    ApprovalStatus,
    AuditAction,
    AuditEntity,
    Fund,
    PersonRole,
    TransactionType,
    UserRole,
)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class User(Base, TimestampMixin):
    """A church user who can sign in to the shared ledger."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    #: What this account is for. Authorisation reads this, never a flag on the
    #: row, so there is exactly one place that decides what a role may do.
    role: Mapped[UserRole] = enum_column(
        UserRole, "user_role", server_default=text(f"'{UserRole.ACCOUNTANT}'")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<User {self.email}>"


class RefreshToken(Base):
    """Opaque refresh token record, enabling logout and revocation."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="refresh_tokens")

    @property
    def is_valid(self) -> bool:
        return self.revoked_at is None


class PasswordResetToken(Base):
    """Single-use password reset token."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()

    @property
    def is_valid(self) -> bool:
        return self.used_at is None


class Person(Base, TimestampMixin):
    """A member, guest, supplier, employee or system user in the directory."""

    __tablename__ = "people"
    __table_args__ = (
        Index("ix_people_lower_name", text("lower(name)")),
        Index("ix_people_role", "role"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    role: Mapped[PersonRole] = enum_column(PersonRole, "person_role")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(40), default="", server_default="")
    #: Optional for members and guests, mandatory for suppliers, employees and
    #: users - see EMAIL_REQUIRED_ROLES.
    email: Mapped[str] = mapped_column(String(320), default="", server_default="")
    category: Mapped[str] = mapped_column(String(60), default="", server_default="")
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Person {self.name} ({self.role})>"


def _pct_deduction_index() -> Index:
    """Partial index covering only the Tithe % Deduction history.

    Built in a helper because SQLAlchemy takes the PostgreSQL-specific
    ``postgresql_where`` as a dialect keyword argument, which mypy cannot match
    against the positional overloads of ``Index``.
    """
    options: dict[str, Any] = {"postgresql_where": text("category = 'Percentage Deduction'")}
    return Index("ix_transactions_pct_deduction", "date", **options)


class Transaction(Base, TimestampMixin):
    """A single money-in or money-out entry.

    Transfers are stored as two rows (one ``expense`` and one ``income``)
    sharing the same ``transfer_id``.
    """

    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "(category = 'Transfer') = (transfer_id IS NOT NULL)",
            name="transfer_category_matches_link",
        ),
        # Declared here as well as in migration 0002 so the model and the database
        # agree; otherwise create_all (tests) would build a weaker schema than the
        # real one, and autogenerate would try to drop the live constraints.
        CheckConstraint(
            "(pct IS NULL) = (base_total IS NULL)",
            name="pct_base_total_pair",
        ),
        CheckConstraint(
            "(pct IS NULL) OR category = 'Percentage Deduction'",
            name="pct_on_deduction",
        ),
        Index("ix_transactions_date_desc", text("date DESC")),
        Index("ix_transactions_lower_party", text("lower(party)")),
        Index("ix_transactions_category", "category"),
        Index("ix_transactions_transfer_id", "transfer_id"),
        Index("ix_transactions_account", "account"),
        Index("ix_transactions_fund", "fund"),
        Index("ix_transactions_person_id", "person_id"),
        _pct_deduction_index(),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    type: Mapped[TransactionType] = enum_column(TransactionType, "transaction_type")
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    party: Mapped[str] = mapped_column(String(200), default="", server_default="")
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    fund: Mapped[str] = mapped_column(String(60), default="", server_default="")
    account: Mapped[Account] = enum_column(Account, "account")
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    date: Mapped[date] = mapped_column(Date, nullable=False)
    transfer_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # Only set on "Percentage Deduction" rows: the percentage that was applied
    # and the tithe total it was taken from, so the history keeps the arithmetic
    # that produced the amount even if the Sunday's tithes are later edited.
    pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    base_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    #: Who the money came from, for income. Money in is always recorded against
    #: someone already in the directory rather than a typed-in name, which is what
    #: makes "who gave what" answerable without matching on spelling.
    #:
    #: Null on three kinds of row, none of them a gift: the income leg of a
    #: transfer (the church's own money changing account), and entries written
    #: before this column existed. Those keep ``party`` as the only record of the
    #: name, and are nulled rather than deleted if the person is removed - see the
    #: ``ondelete`` below.
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("people.id", ondelete="SET NULL"), nullable=True
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Transaction {self.type} {self.category} {self.amount}>"


#: Exported for convenience when building type-annotated column tuples.
FUND_ENUM_TYPE = Fund


# --------------------------------------------------------------------------- #
# Approvals and audit trail
# --------------------------------------------------------------------------- #
class ApprovalRequest(Base, TimestampMixin):
    """A money-out request waiting for an administrator to approve it.

    A pending request deliberately has no effect on the ledger: no balance, report
    or trial balance moves until it is approved, at which point a real
    :class:`Transaction` is written and linked back through ``transaction_id``.

    Keeping the request as its own row (rather than a status column on
    ``transactions``) means the ledger only ever contains approved money, and the
    who-asked / who-approved pair survives for good.
    """

    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_approval_requests_status", "status"),
        Index("ix_approval_requests_created_at_desc", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    status: Mapped[ApprovalStatus] = enum_column(ApprovalStatus, "approval_status")
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    party: Mapped[str] = mapped_column(String(200), default="", server_default="")
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    fund: Mapped[str] = mapped_column(String(60), default="", server_default="")
    account: Mapped[Account] = enum_column(Account, "account")
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: When the money is expected to leave, which is not necessarily today.
    date: Mapped[date] = mapped_column(Date, nullable=False)

    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str] = mapped_column(
        String(500), default="", server_default=""
    )
    #: Set when approved: the ledger row this request turned into.
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<ApprovalRequest {self.category} {self.amount} ({self.status})>"


class AuditEntry(Base):
    """One immutable line in the audit trail.

    Append-only by convention: nothing in the application updates or deletes these
    rows. ``actor_email`` is denormalised on purpose so the entry still names a
    person after their account is removed, and ``changes`` holds the before/after
    values for an edit so the log shows *what* changed rather than only that
    something did.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_created_at_desc", text("created_at DESC")),
        Index("ix_audit_log_actor", "actor_id"),
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_email: Mapped[str] = mapped_column(String(320), default="", server_default="")
    action: Mapped[AuditAction] = enum_column(AuditAction, "audit_action")
    entity_type: Mapped[AuditEntity] = enum_column(AuditEntity, "audit_entity")
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    changes: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[str] = mapped_column(String(45), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<AuditEntry {self.action} {self.entity_type}>"
