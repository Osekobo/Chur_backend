"""SQLAlchemy declarative base and shared column helpers."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import TypeVar

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql.elements import TextClause

# Explicit naming convention so Alembic can autogenerate reversible migrations.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_EnumT = TypeVar("_EnumT", bound=Enum)


def enum_column(
    enum_cls: type[_EnumT], name: str, *, server_default: TextClause | None = None
) -> Mapped[_EnumT]:
    """Build a PostgreSQL enum column that stores the enum *values*.

    The native enum type is created by Alembic, not by the ORM. ``server_default``
    is passed through so the model can declare one that matches the migration -
    ``compare_server_default=True`` in the Alembic env would otherwise read the
    model and the database as having drifted apart.
    """
    return mapped_column(
        SAEnum(
            enum_cls,
            name=name,
            native_enum=True,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda e: [member.value for member in e],
        ),
        server_default=server_default,
    )


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def created_at_column() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=False
    )


def updated_at_column() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
