"""Ad-hoc check that the Alembic chain applies and rolls back cleanly.

Runs against a throwaway database (``migration_check``) which is dropped first and
last, so the development database is never touched.
"""

from __future__ import annotations

import os
import sys

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

DB = "migration_check"
ADMIN = "postgresql://postgres:12039@localhost:5432/postgres"

connection = psycopg2.connect(ADMIN)
connection.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
cursor = connection.cursor()
cursor.execute(f"DROP DATABASE IF EXISTS {DB}")
cursor.execute(f"CREATE DATABASE {DB}")
cursor.close()
connection.close()

os.environ["ENVIRONMENT"] = "test"
os.environ["DATABASE_URL"] = (
    f"postgresql+asyncpg://postgres:12039@localhost:5432/{DB}"
)
# DATABASE_SYNC_URL is deliberately left unset: Settings derives it from
# DATABASE_URL by swapping asyncpg for psycopg2. Setting it to a bare
# "postgresql://" URL would make SQLAlchemy reach for psycopg3, which is not a
# dependency of this project.
os.environ.pop("DATABASE_SYNC_URL", None)
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-for-validation-123")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

config = Config("alembic.ini")

SYNC_URL = f"postgresql+psycopg2://postgres:12039@localhost:5432/{DB}"

print("--- upgrade head ---")
command.upgrade(config, "head")

from sqlalchemy import create_engine, text  # noqa: E402

engine = create_engine(SYNC_URL, future=True)
with engine.connect() as conn:
    tables = [
        row[0]
        for row in conn.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
                " ORDER BY tablename"
            )
        )
    ]
    print("tables:", ", ".join(tables))
    enums = [
        row[0]
        for row in conn.execute(
            text("SELECT typname FROM pg_type WHERE typtype='e' ORDER BY typname")
        )
    ]
    print("enums:", ", ".join(enums))
    for expected in ("audit_log", "approval_requests"):
        assert expected in tables, f"missing {expected}"
    for expected in ("audit_action", "audit_entity", "approval_status"):
        assert expected in enums, f"missing enum {expected}"
engine.dispose()

print("--- downgrade 0002_pct_deduction ---")
command.downgrade(config, "0002_pct_deduction")
engine = create_engine(SYNC_URL, future=True)
with engine.connect() as conn:
    tables = {
        row[0]
        for row in conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        )
    }
assert "audit_log" not in tables, "audit_log survived downgrade"
assert "approval_requests" not in tables, "approval_requests survived downgrade"
engine.dispose()

print("--- upgrade head again ---")
command.upgrade(config, "head")

print("--- downgrade base ---")
command.downgrade(config, "base")
engine = create_engine(SYNC_URL, future=True)
with engine.connect() as conn:
    remaining = [
        row[0]
        for row in conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        )
    ]
    enums = [
        row[0]
        for row in conn.execute(
            text("SELECT typname FROM pg_type WHERE typtype='e'")
        )
    ]
engine.dispose()
# Alembic keeps its own bookkeeping table, so it is expected to survive.
print("tables after full downgrade:", remaining or "(none)")
print("enums after full downgrade:", enums or "(none)")
leftover = [name for name in remaining if name != "alembic_version"]
if leftover or enums:
    print(f"FAIL: downgrade did not fully clean up: tables={leftover} enums={enums}")
    sys.exit(1)

connection = psycopg2.connect(ADMIN)
connection.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
cursor = connection.cursor()
cursor.execute(f"DROP DATABASE IF EXISTS {DB}")
cursor.close()
connection.close()
print("OK: migrations apply, downgrade and re-apply cleanly")
