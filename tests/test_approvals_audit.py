"""Tests for the money-out approval workflow and the audit trail.

The two features share a test file because the interesting assertions are about
the seam between them: approving a request writes a ledger row *and* leaves a
trail of who asked and who authorised it.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from tests.conftest import IdentityClient

API = "/api/v1"

TODAY = date.today().isoformat()


def unique_email() -> str:
    import uuid

    return f"user-{uuid.uuid4().hex[:10]}@example.com"


def money_out(**overrides: object) -> dict[str, object]:
    """A valid expense request payload."""
    payload: dict[str, object] = {
        "category": "Expenses",
        "party": "Kenya Power",
        "amount": "1500.00",
        "fund": "General Fund",
        "account": "Bank",
        "notes": "October electricity bill",
        "date": TODAY,
    }
    payload.update(overrides)
    return payload


def transaction_count(client: TestClient) -> int:
    response = client.get(f"{API}/transactions")
    assert response.status_code == 200, response.text
    return len(response.json())


class TestApprovals:
    def test_a_request_starts_pending(
        self, accountant_client: IdentityClient, clean_db: None
    ) -> None:
        response = accountant_client.post(f"{API}/approvals", json=money_out())
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "pending"
        assert body["is_pending"] is True
        assert body["amount"] == "1500.00"
        assert body["decided_by_id"] is None
        assert body["transaction_id"] is None

    def test_a_pending_request_does_not_touch_the_ledger(
        self, accountant_client: IdentityClient, clean_db: None
    ) -> None:
        before = transaction_count(accountant_client)
        assert accountant_client.post(f"{API}/approvals", json=money_out()).status_code == 201
        assert transaction_count(accountant_client) == before

    def test_any_signed_in_user_may_raise_a_request(
        self, client: TestClient, clean_db: None
    ) -> None:
        email = unique_email()
        client.post(
            f"{API}/auth/register",
            json={"email": email, "full_name": "Raiser", "password": "StrongPass123"},
        )
        token = client.post(
            f"{API}/auth/login", json={"email": email, "password": "StrongPass123"}
        ).json()["tokens"]["access_token"]
        client.headers.update({"Authorization": f"Bearer {token}"})

        response = client.post(f"{API}/approvals", json=money_out())
        assert response.status_code == 201, response.text
        # An accountant may see the queue, but the buttons on it are decided by
        # the API, not hidden in the interface - see the secretary test below.
        assert client.get(f"{API}/approvals").status_code == 200

    def test_a_secretary_may_raise_but_not_decide(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        """The split that makes the role worth having: raise without authorising."""
        raised = secretary_client.post(f"{API}/approvals", json=money_out())
        assert raised.status_code == 201, raised.text
        approval = raised.json()

        assert secretary_client.get(f"{API}/approvals").status_code == 200
        assert (
            secretary_client.post(
                f"{API}/approvals/{approval['id']}/approve", json={"note": "ok"}
            ).status_code
            == 403
        )
        assert (
            secretary_client.post(
                f"{API}/approvals/{approval['id']}/reject", json={"note": "no"}
            ).status_code
            == 403
        )
        # Still pending, and still in no ledger: a refused decision moves nothing.
        queue = secretary_client.get(f"{API}/approvals", params={"status": "pending"}).json()
        assert [row["id"] for row in queue] == [approval["id"]]
        assert transaction_count(secretary_client) == 0

    def test_approval_records_the_expense_in_the_ledger(
        self, accountant_client: IdentityClient, admin_client: TestClient, clean_db: None
    ) -> None:
        """The core promise: an approved request becomes a real Money Out row."""
        raise_response = accountant_client.post(f"{API}/approvals", json=money_out())
        assert raise_response.status_code == 201, raise_response.text
        approval = raise_response.json()

        response = admin_client.post(
            f"{API}/approvals/{approval['id']}/approve", json={"note": "Checked the bill"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "approved"
        assert body["transaction_id"] is not None
        assert body["decision_note"] == "Checked the bill"
        assert body["decided_by_id"] is not None

        # The ledger now holds exactly the requested expense.
        entries = admin_client.get(
            f"{API}/transactions", params={"type": "expense", "category": "Expenses"}
        ).json()
        assert len(entries) == 1
        assert Decimal(entries[0]["amount"]) == Decimal("1500.00")
        assert entries[0]["party"] == "Kenya Power"
        assert entries[0]["id"] == body["transaction_id"]

    def test_approval_moves_the_balance_that_a_rejection_does_not(
        self, accountant_client: IdentityClient, admin_client: TestClient, clean_db: None
    ) -> None:
        approved = accountant_client.post(
            f"{API}/approvals", json=money_out(amount="500.00")
        ).json()
        rejected = accountant_client.post(
            f"{API}/approvals", json=money_out(amount="900.00")
        ).json()

        assert (
            admin_client.post(
                f"{API}/approvals/{approved['id']}/approve", json={"note": "ok"}
            ).status_code
            == 200
        )
        assert (
            admin_client.post(
                f"{API}/approvals/{rejected['id']}/reject", json={"note": "Not this quarter"}
            ).status_code
            == 200
        )

        balance = admin_client.get(f"{API}/transactions/accounts/balances")
        balances = {row["key"]: Decimal(row["total"]) for row in balance.json()}
        # Only the approved 500.00 is reflected; the rejected 900.00 never moved.
        assert balances["Bank"] == Decimal("-500.00")

    def test_a_request_cannot_be_decided_twice(
        self, accountant_client: IdentityClient, admin_client: TestClient, clean_db: None
    ) -> None:
        approval = accountant_client.post(f"{API}/approvals", json=money_out()).json()
        assert (
            admin_client.post(
                f"{API}/approvals/{approval['id']}/approve", json={"note": "ok"}
            ).status_code
            == 200
        )

        # A retried approve, or an approve after a reject, must be refused - this
        # is what stops a double-click posting the expense twice.
        second = admin_client.post(
            f"{API}/approvals/{approval['id']}/approve", json={"note": "again"}
        )
        assert second.status_code == 409
        assert transaction_count(admin_client) == 1

    def test_reject_after_approve_is_refused(
        self, accountant_client: IdentityClient, admin_client: TestClient, clean_db: None
    ) -> None:
        approval = accountant_client.post(f"{API}/approvals", json=money_out()).json()
        admin_client.post(f"{API}/approvals/{approval['id']}/approve", json={"note": "ok"})
        response = admin_client.post(
            f"{API}/approvals/{approval['id']}/reject", json={"note": "changed my mind"}
        )
        assert response.status_code == 409
        assert transaction_count(admin_client) == 1

    def test_filtering_by_status_and_the_badge_count(
        self, accountant_client: IdentityClient, admin_client: TestClient, clean_db: None
    ) -> None:
        first = accountant_client.post(f"{API}/approvals", json=money_out()).json()
        second = accountant_client.post(f"{API}/approvals", json=money_out(amount="42.00")).json()
        admin_client.post(f"{API}/approvals/{first['id']}/approve", json={"note": ""})

        counts = admin_client.get(f"{API}/approvals/count").json()
        assert counts == {"pending": 1, "approved": 1, "rejected": 0, "total": 2}

        pending = admin_client.get(f"{API}/approvals", params={"status": "pending"}).json()
        assert [row["id"] for row in pending] == [second["id"]]

    def test_mine_returns_only_your_own_requests(
        self, accountant_client: IdentityClient, admin_client: TestClient, clean_db: None
    ) -> None:
        mine = accountant_client.post(f"{API}/approvals", json=money_out()).json()
        admin_client.post(f"{API}/approvals", json=money_out(amount="10.00"))

        rows = accountant_client.get(f"{API}/approvals", params={"mine": "true"}).json()
        assert [row["id"] for row in rows] == [mine["id"]]

    def test_an_invalid_category_is_refused_at_the_gate(
        self, accountant_client: IdentityClient, clean_db: None
    ) -> None:
        # "Tithes" is income, not an expense, so a request may not ask for it -
        # otherwise approval would have to reject it later.
        response = accountant_client.post(f"{API}/approvals", json=money_out(category="Tithes"))
        assert response.status_code == 422

    def test_a_zero_amount_is_refused(self, auth_client: TestClient, clean_db: None) -> None:
        assert auth_client.post(f"{API}/approvals", json=money_out(amount="0")).status_code == 422
        assert (
            auth_client.post(f"{API}/approvals", json=money_out(amount="-5.00")).status_code == 422
        )

    def test_the_ledger_requires_authentication(self, client: TestClient, clean_db: None) -> None:
        assert client.get(f"{API}/approvals").status_code == 401
        assert client.post(f"{API}/approvals", json=money_out()).status_code == 401

    def test_a_missing_request_is_a_404(self, admin_client: TestClient, clean_db: None) -> None:
        import uuid

        response = admin_client.post(f"{API}/approvals/{uuid.uuid4()}/approve", json={"note": ""})
        assert response.status_code == 404

    def test_a_future_date_is_accepted(self, auth_client: TestClient, clean_db: None) -> None:
        """Money can be requested before it leaves, e.g. a bill due next month."""
        future = (date.today() + timedelta(days=30)).isoformat()
        response = auth_client.post(f"{API}/approvals", json=money_out(date=future))
        assert response.status_code == 201, response.text
        assert response.json()["date"] == future


class TestAuditTrail:
    def _entries(self, client: TestClient, **params: object) -> list[dict[str, object]]:
        response = client.get(f"{API}/audit", params=params)
        assert response.status_code == 200, response.text
        return response.json()["items"]

    def test_reading_the_trail_is_admin_only(
        self, accountant_client: IdentityClient, admin_client: TestClient, clean_db: None
    ) -> None:
        assert accountant_client.get(f"{API}/audit").status_code == 403
        assert admin_client.get(f"{API}/audit").status_code == 200

    def test_the_trail_requires_authentication(self, client: TestClient, clean_db: None) -> None:
        assert client.get(f"{API}/audit").status_code == 401

    def test_a_sign_in_is_recorded(self, client: TestClient, clean_db: None) -> None:
        email = unique_email()
        client.post(
            f"{API}/auth/register",
            json={"email": email, "full_name": "Audited", "password": "StrongPass123"},
        )
        client.post(f"{API}/auth/login", json={"email": email, "password": "StrongPass123"})

        entries = self._entries(_admin(client), action="login")
        assert any(email in str(entry["actor_email"]) for entry in entries)

    def test_a_failed_sign_in_is_recorded_with_the_attempted_email(
        self, client: TestClient, clean_db: None
    ) -> None:
        email = unique_email()
        response = client.post(
            f"{API}/auth/login", json={"email": email, "password": "WrongPass123"}
        )
        assert response.status_code == 401

        entries = self._entries(_admin(client), action="login_failed")
        matched = [entry for entry in entries if entry["actor_email"] == email]
        assert matched, "a failed sign-in attempt should be in the trail"
        # The password is never stored, only the fact of the attempt.
        assert "password" not in str(matched[0]).lower()

    def test_recording_an_expense_names_the_actor_and_the_amount(
        self, accountant_client: IdentityClient, admin_client: TestClient, clean_db: None
    ) -> None:
        accountant_client.post(
            f"{API}/transactions",
            json={
                "type": "expense",
                "category": "Expenses",
                "party": "Kenya Power",
                "amount": "250.00",
                "fund": "General Fund",
                "account": "Bank",
                "date": TODAY,
            },
        )
        entries = self._entries(admin_client, entity="transaction", action="create")
        assert entries, "recording an expense should leave a trail entry"
        assert "250" in str(entries[0]["changes"])

    def test_deleting_a_transaction_keeps_what_it_was(
        self, accountant_client: IdentityClient, admin_client: TestClient, clean_db: None
    ) -> None:
        created = accountant_client.post(
            f"{API}/transactions",
            json={
                "type": "expense",
                "category": "Expenses",
                "party": "Kenya Power",
                "amount": "310.00",
                "fund": "General Fund",
                "account": "Bank",
                "date": TODAY,
            },
        ).json()
        assert accountant_client.delete(f"{API}/transactions/{created['id']}").status_code == 200

        entries = self._entries(admin_client, action="delete")
        assert len(entries) == 1
        # The row is gone, but the log still describes it.
        assert Decimal(str(entries[0]["changes"]["amount"])) == Decimal("310.00")
        assert entries[0]["changes"]["party"] == "Kenya Power"

    def test_an_approval_leaves_both_an_approval_and_a_ledger_line(
        self, accountant_client: IdentityClient, admin_client: TestClient, clean_db: None
    ) -> None:
        approval = accountant_client.post(f"{API}/approvals", json=money_out()).json()
        admin_client.post(f"{API}/approvals/{approval['id']}/approve", json={"note": "ok"})

        approval_entries = self._entries(admin_client, action="approve")
        assert len(approval_entries) == 1
        assert approval_entries[0]["entity_type"] == "approval"
        assert approval_entries[0]["changes"]["status"] == ["pending", "approved"]

        # Approving created a transaction, and that is audited too.
        ledger_entries = self._entries(admin_client, entity="transaction", action="create")
        assert len(ledger_entries) == 1
        assert "approved request" in str(ledger_entries[0]["summary"])

    def test_a_role_change_is_recorded_as_a_role_change(
        self, admin_client: TestClient, clean_db: None
    ) -> None:
        created = admin_client.post(
            f"{API}/users",
            json={
                "email": unique_email(),
                "full_name": "Promote Me",
                "password": "StrongPass123",
                "role": "secretary",
            },
        ).json()

        admin_client.patch(f"{API}/users/{created['id']}", json={"role": "admin"})

        entries = self._entries(admin_client, action="role_change")
        assert len(entries) == 1
        assert entries[0]["changes"]["role"] == ["secretary", "admin"]

    def test_a_password_reset_is_recorded(self, admin_client: TestClient, clean_db: None) -> None:
        created = admin_client.post(
            f"{API}/users",
            json={
                "email": unique_email(),
                "full_name": "Forgot",
                "password": "StrongPass123",
                "role": "secretary",
            },
        ).json()
        admin_client.post(
            f"{API}/users/{created['id']}/password", json={"new_password": "BrandNew123"}
        )
        entries = self._entries(admin_client, action="password_reset")
        assert len(entries) == 1
        assert created["email"] in str(entries[0]["summary"])

    def test_a_no_op_update_writes_nothing(self, admin_client: TestClient, clean_db: None) -> None:
        """Re-saving identical values should not claim something changed."""
        created = admin_client.post(
            f"{API}/users",
            json={
                "email": unique_email(),
                "full_name": "Same",
                "password": "StrongPass123",
                "role": "secretary",
            },
        ).json()
        before = len(self._entries(admin_client, entity="user"))
        admin_client.patch(f"{API}/users/{created['id']}", json={"full_name": created["full_name"]})
        assert len(self._entries(admin_client, entity="user")) == before

    def test_pagination_reports_a_total(
        self, accountant_client: IdentityClient, admin_client: TestClient, clean_db: None
    ) -> None:
        for index in range(5):
            accountant_client.post(
                f"{API}/transactions",
                json={
                    "type": "expense",
                    "category": "Expenses",
                    "amount": f"{index + 1}.00",
                    "fund": "General Fund",
                    "account": "Bank",
                    "date": TODAY,
                },
            )
        page = admin_client.get(f"{API}/audit", params={"page": 1, "page_size": 2}).json()
        assert page["total"] == 5
        assert len(page["items"]) == 2
        assert page["pages"] == 3

        second = admin_client.get(f"{API}/audit", params={"page": 2, "page_size": 2}).json()
        assert len(second["items"]) == 2
        # Pages must not overlap, and the newest entry has to be on page one.
        first_ids = {item["id"] for item in page["items"]}
        assert first_ids.isdisjoint({item["id"] for item in second["items"]})

    def test_newest_entries_come_first(
        self, accountant_client: IdentityClient, admin_client: TestClient, clean_db: None
    ) -> None:
        accountant_client.post(
            f"{API}/transactions",
            json={
                "type": "expense",
                "category": "Expenses",
                "amount": "1.00",
                "fund": "General Fund",
                "account": "Bank",
                "date": TODAY,
            },
        )
        items = self._entries(admin_client, entity="transaction")
        stamps = [str(item["created_at"]) for item in items]
        assert stamps == sorted(stamps, reverse=True)

    def test_the_trail_cannot_be_edited_through_the_api(
        self, admin_client: TestClient, clean_db: None
    ) -> None:
        """Even an administrator has no write verb on the audit log."""
        import uuid

        entry_id = uuid.uuid4()
        for method, path in (
            ("post", f"{API}/audit"),
            ("put", f"{API}/audit/{entry_id}"),
            ("patch", f"{API}/audit/{entry_id}"),
        ):
            response = getattr(admin_client, method)(path, json={})
            assert response.status_code in (404, 405), f"{method} {path} -> {response.status_code}"

        # httpx has no body on DELETE, so it is sent on its own.
        deleted = admin_client.delete(f"{API}/audit/{entry_id}")
        assert deleted.status_code in (404, 405), deleted.status_code

    def test_the_actor_ip_is_recorded(
        self, accountant_client: IdentityClient, admin_client: TestClient, clean_db: None
    ) -> None:
        accountant_client.post(
            f"{API}/transactions",
            json={
                "type": "expense",
                "category": "Expenses",
                "amount": "5.00",
                "fund": "General Fund",
                "account": "Bank",
                "date": TODAY,
            },
        )
        entries = self._entries(admin_client, entity="transaction")
        assert entries[0]["ip_address"], "the caller address should be captured"

    def test_search_matches_the_summary_and_actor(
        self, accountant_client: IdentityClient, admin_client: TestClient, clean_db: None
    ) -> None:
        accountant_client.post(
            f"{API}/transactions",
            json={
                "type": "expense",
                "category": "Expenses",
                "party": "Kenya Power",
                "amount": "77.00",
                "fund": "General Fund",
                "account": "Bank",
                "date": TODAY,
            },
        )
        found = self._entries(admin_client, search="Kenya Power")
        assert found
        assert not self._entries(admin_client, search="no-such-thing-anywhere")


def _admin(client: TestClient) -> TestClient:
    """Promote a freshly registered account and sign the client in as it.

    Used by tests that need to read the trail without depending on a fixture
    that truncates ``users``.
    """
    import os

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    email = unique_email()
    registered = client.post(
        f"{API}/auth/register",
        json={"email": email, "full_name": "Trail Reader", "password": "StrongPass123"},
    )
    assert registered.status_code == 201, registered.text
    user_id = registered.json()["user"]["id"]

    engine = create_engine(os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2"), future=True)
    with Session(engine) as session:
        session.execute(
            text("UPDATE users SET role = 'admin' WHERE id = :id"), {"id": str(user_id)}
        )
        session.commit()
    engine.dispose()

    token = registered.json()["tokens"]["access_token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client
