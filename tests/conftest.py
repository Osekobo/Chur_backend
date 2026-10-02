"""Shared pytest fixtures.

The whole test session runs against a dedicated ``churchfinance_test``
database so running the suite can never touch development data. The DSN is
injected into the environment *before* the application modules are imported.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

# --- Must happen before any `app.*` import so Settings picks it up. ---------
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:12039@localhost:5432/churchfinance_test",
)
os.environ.setdefault(
    "DATABASE_SYNC_URL",
    "postgresql+psycopg2://postgres:12039@localhost:5432/churchfinance_test",
)
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-for-validation-123")
os.environ.setdefault("RETURN_RESET_LINK_IN_RESPONSE", "true")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import httpx
import pytest
from app.core.config import settings
from app.db import models  # noqa: F401  (registers the tables)
from app.db.base import Base
from fastapi.testclient import TestClient
from httpx import Headers
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

TEST_USER = {
    "email": "treasurer@example.com",
    "full_name": "Test Treasurer",
    "password": "TestPass123",
}


@pytest.fixture(scope="session", autouse=True)
def _schema() -> Iterator[None]:
    """Create the schema for the test database and drop it afterwards.

    Uses a blocking engine because psycopg2 is not tied to an event loop.
    """
    engine = create_engine(settings.sync_database_url, future=True)
    Base.metadata.drop_all(bind=engine, checkfirst=True)
    Base.metadata.create_all(bind=engine, checkfirst=True)
    try:
        yield
    finally:
        Base.metadata.drop_all(bind=engine, checkfirst=True)
        engine.dispose()


@pytest.fixture()
def clean_db(_schema: None) -> Iterator[None]:
    """Truncate the transactional tables so each test starts from zero.

    ``approval_requests`` and ``audit_log`` are included: both are written by
    ordinary requests, so a test that leaves a pending approval or a log entry
    behind would change what the next test's counts and lists report. CASCADE
    clears the dependents (``audit_log.actor_id`` and
    ``approval_requests.requested_by_id`` both point at ``users``).
    """
    engine = create_engine(settings.sync_database_url, future=True)
    with Session(engine) as session:
        session.execute(
            text(
                "TRUNCATE approval_requests, audit_log, transactions, people"
                " RESTART IDENTITY CASCADE"
            )
        )
        session.commit()
    yield
    engine.dispose()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_client(client: TestClient) -> Iterator[TestClient]:
    """A client signed in as a user created for this test session."""
    email = TEST_USER["email"]
    response = client.post(
        f"{settings.API_V1_PREFIX}/auth/login",
        json={"email": email, "password": TEST_USER["password"]},
    )
    if response.status_code != 200:
        response = client.post(
            f"{settings.API_V1_PREFIX}/auth/register",
            json={
                "email": email,
                "full_name": TEST_USER["full_name"],
                "password": TEST_USER["password"],
            },
        )
        assert response.status_code == 201, response.text
    access_token = response.json()["tokens"]["access_token"]
    client.headers.update({"Authorization": f"Bearer {access_token}"})
    yield client


def _register(client: TestClient, email: str, full_name: str) -> tuple[str, str]:
    """Register an account; return its access token and id.

    Shared by the role fixtures below. Self-registration cannot pick a role, so the
    account lands as an accountant and each fixture then writes the role it stands
    for straight to the database - the same ``UPDATE`` an operator runs in psql,
    since no API route can promote an account that does not exist yet.
    """
    response = client.post(
        f"{settings.API_V1_PREFIX}/auth/register",
        json={"email": email, "full_name": full_name, "password": ROLE_PASSWORD},
    )
    assert response.status_code == 201, response.text
    return (
        str(response.json()["tokens"]["access_token"]),
        str(response.json()["user"]["id"]),
    )


def _set_role(user_id: str, role: str) -> None:
    engine = create_engine(settings.sync_database_url, future=True)
    with Session(engine) as session:
        session.execute(
            text("UPDATE users SET role = :role WHERE id = :id"),
            {"role": role, "id": user_id},
        )
        session.commit()
    engine.dispose()


ROLE_PASSWORD = "RolePass123"

ACCOUNTANT_USER = {
    "email": "accountant@example.com",
    "full_name": "Church Accountant",
}

SECRETARY_USER = {
    "email": "secretary@example.com",
    "full_name": "Church Secretary",
}


@pytest.fixture()
def accountant_client(client: TestClient, clean_users: None) -> Iterator[IdentityClient]:
    """An identity holding the money role: can spend, transfer, decide approvals.

    No ``UPDATE`` needed - self-registration already lands here.
    """
    token, _ = _register(client, **ACCOUNTANT_USER)
    yield IdentityClient(client, token)


@pytest.fixture()
def secretary_client(client: TestClient, clean_users: None) -> Iterator[IdentityClient]:
    """An identity holding the front-office role: directory and money in only.

    The role is written directly because a secretary cannot manage accounts, so
    there is no API route through which a test could grant it.
    """
    token, user_id = _register(client, **SECRETARY_USER)
    _set_role(user_id, "secretary")
    yield IdentityClient(client, token)


class IdentityClient:
    """A ``TestClient`` view that authenticates as one fixed account.

    A test needs to be two people at once - a secretary may raise a request, then
    an accountant approves it. Two separate ``TestClient`` objects cannot
    express that here: the app owns a single async engine and each ``TestClient``
    drives the app in its own event loop, so a pooled asyncpg connection opened in
    one loop is reused in the other and the suite dies with "got Future attached
    to a different loop".

    So there is one ``TestClient``, and this proxy pins the ``Authorization``
    header to its own token for every call. It is passed per-request rather than
    written onto ``client.headers``, so several identities can share the same
    underlying client without stomping on each other.
    """

    def __init__(self, client: TestClient, access_token: str) -> None:
        self._client = client
        self._token = access_token

    def _headers(self) -> Headers:
        # Must be a case-insensitive Headers, not a plain dict. ``client.headers``
        # stores its keys lower-cased, so ``{**client.headers, "Authorization": ...}``
        # yields *two* authorization entries and the stale one wins - the caller
        # silently keeps acting as whichever identity was last set on the shared
        # client. Setting a key on Headers replaces it instead.
        headers = Headers(self._client.headers)
        headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        return self._client.request(method, url, headers=self._headers(), **kwargs)  # type: ignore[arg-type]

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: object) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: object) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: object) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: object) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)


def insert_person(name: str, role: str = "Member", **fields: str) -> dict[str, str]:
    """Put one person into the directory and return their id and name.

    Written straight to the database rather than through ``POST /people`` because
    creating people is a secretary's job and most of these tests are signed in as an
    accountant - who may read the directory but may not change it.

    A plain function rather than a fixture, so the helpers inside a test module can
    call it mid-test; every caller runs after ``clean_db`` has already truncated, so
    the row is never wiped by the next test's setup.

    Hands back the row already there if that name is in the directory in that role:
    ``people`` is unique on (role, lower(name)), and a shared helper called twice
    from two tests means one person, not two. Tests about duplicate refusal go
    through the API instead, where the check under test actually lives.
    """
    from app.db.models import Person
    from app.enums import PersonRole

    engine = create_engine(settings.sync_database_url, future=True)
    try:
        with Session(engine) as session:
            existing = session.scalar(
                select(Person).where(
                    Person.role == PersonRole(role), func.lower(Person.name) == name.lower()
                )
            )
            if existing is not None:
                return {"id": str(existing.id), "name": existing.name, "role": role}
            person = Person(name=name, role=PersonRole(role), **fields)
            session.add(person)
            session.commit()
            session.refresh(person)
            return {"id": str(person.id), "name": person.name, "role": role}
    finally:
        engine.dispose()


@pytest.fixture()
def directory(clean_db: None) -> Iterator[Callable[..., dict[str, str]]]:
    """Insert directory rows before the test body runs.

    Depends on ``clean_db`` so the rows are always written after the truncation,
    whichever order a test declares its fixtures in.
    """
    yield insert_person


@pytest.fixture()
def member(directory: Callable[..., dict[str, str]]) -> dict[str, str]:
    """One person in the directory, for tests that record money in.

    Money in names a giver from the directory, so any test posting an income entry
    needs somebody to name.
    """
    return directory("Mary Achieng")


@pytest.fixture()
def clean_users() -> Iterator[None]:
    """Drop every account before and after the test.

    ``users`` is not covered by ``clean_db`` because the signed-in fixtures need
    their account to survive. The administrator tests need the opposite: a known
    count of administrators, so "the last administrator cannot be demoted" can be
    asserted for real. CASCADE clears the dependent refresh tokens too.
    """
    engine = create_engine(settings.sync_database_url, future=True)
    with Session(engine) as session:
        session.execute(text("TRUNCATE users CASCADE"))
        session.commit()
    yield
    engine.dispose()


@pytest.fixture()
def admin_client(client: TestClient, clean_users: None) -> Iterator[TestClient]:
    """A client signed in as an administrator.

    The role is written straight to the database rather than granted through the
    API, because a newly registered account has no administrator rights and so
    could not promote itself - this is the very same ``UPDATE`` an operator runs
    in psql to create the first administrator.

    The role is re-read from the database on every request, so the assertion at
    the end is a real check that it took effect and not just a cached value.
    """
    register = client.post(
        f"{settings.API_V1_PREFIX}/auth/register",
        json={
            "email": ADMIN_USER["email"],
            "full_name": ADMIN_USER["full_name"],
            "password": ADMIN_USER["password"],
        },
    )
    assert register.status_code == 201, register.text
    _set_role(register.json()["user"]["id"], "admin")

    client.headers.update({"Authorization": f"Bearer {register.json()['tokens']['access_token']}"})
    assert client.get(f"{settings.API_V1_PREFIX}/users").status_code == 200

    yield client


ADMIN_USER = {
    "email": "admin@example.com",
    "full_name": "Test Administrator",
    "password": "AdminPass123",
}
