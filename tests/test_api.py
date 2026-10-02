"""API tests covering authentication, the ledger, people and reporting."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.enums import TITHE_SCOPE_ALL, TRANSFER_CATEGORY, Account
from app.schemas.transaction import PctDeductionCreate
from fastapi.testclient import TestClient

from tests.conftest import TEST_USER, IdentityClient

API = "/api/v1"


def unique_email() -> str:
    import uuid

    return f"user-{uuid.uuid4().hex[:10]}@example.com"


class TestAuth:
    def test_health_endpoints_are_public(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 200
        assert client.get("/health/ready").status_code == 200

    def test_register_returns_tokens_and_user(self, client: TestClient) -> None:
        email = unique_email()
        response = client.post(
            f"{API}/auth/register",
            json={"email": email, "full_name": "Jane Doe", "password": "StrongPass123"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["user"]["email"] == email
        assert body["tokens"]["token_type"] == "bearer"
        assert body["tokens"]["access_token"]

    def test_registration_rejects_weak_passwords(self, client: TestClient) -> None:
        response = client.post(
            f"{API}/auth/register",
            json={"email": unique_email(), "full_name": "Weak", "password": "short"},
        )
        assert response.status_code == 422
        assert "at least 8" in response.json()["detail"].lower()

    def test_duplicate_email_is_rejected(self, client: TestClient) -> None:
        payload = {"email": unique_email(), "full_name": "Dupe", "password": "StrongPass123"}
        assert client.post(f"{API}/auth/register", json=payload).status_code == 201
        assert client.post(f"{API}/auth/register", json=payload).status_code == 409

    def test_login_and_me(self, client: TestClient) -> None:
        email = unique_email()
        client.post(
            f"{API}/auth/register",
            json={"email": email, "full_name": "Login", "password": "StrongPass123"},
        )

        bad = client.post(f"{API}/auth/login", json={"email": email, "password": "wrongpassword"})
        assert bad.status_code == 401

        good = client.post(f"{API}/auth/login", json={"email": email, "password": "StrongPass123"})
        assert good.status_code == 200
        token = good.json()["tokens"]["access_token"]

        me = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["email"] == email

    def test_protected_route_requires_a_token(self, client: TestClient) -> None:
        assert client.get(f"{API}/dashboard").status_code == 401
        assert (
            client.get(f"{API}/dashboard", headers={"Authorization": "Bearer nonsense"}).status_code
            == 401
        )

    def test_refresh_rotates_the_token(self, client: TestClient) -> None:
        email = unique_email()
        client.post(
            f"{API}/auth/register",
            json={"email": email, "full_name": "Rotator", "password": "StrongPass123"},
        )
        login = client.post(
            f"{API}/auth/login", json={"email": email, "password": "StrongPass123"}
        ).json()
        first = login["tokens"]["refresh_token"]

        refreshed = client.post(f"{API}/auth/refresh", json={"refresh_token": first})
        assert refreshed.status_code == 200
        second = refreshed.json()["tokens"]["refresh_token"]
        assert second != first

        # The consumed token can no longer be used.
        assert client.post(f"{API}/auth/refresh", json={"refresh_token": first}).status_code == 401

    def test_logout_revokes_the_refresh_token(self, client: TestClient) -> None:
        email = unique_email()
        client.post(
            f"{API}/auth/register",
            json={"email": email, "full_name": "Bye", "password": "StrongPass123"},
        )
        refresh_token = client.post(
            f"{API}/auth/login", json={"email": email, "password": "StrongPass123"}
        ).json()["tokens"]["refresh_token"]

        assert (
            client.post(f"{API}/auth/logout", json={"refresh_token": refresh_token}).status_code
            == 200
        )
        assert (
            client.post(f"{API}/auth/refresh", json={"refresh_token": refresh_token}).status_code
            == 401
        )

    def test_forgot_password_returns_a_usable_reset_link(self, client: TestClient) -> None:
        email = unique_email()
        client.post(
            f"{API}/auth/register",
            json={"email": email, "full_name": "Forgot", "password": "StrongPass123"},
        )

        response = client.post(f"{API}/auth/forgot-password", json={"email": email})
        assert response.status_code == 200
        reset_url = response.json()["reset_url"]
        assert reset_url and "/reset-password?token=" in reset_url

        token = reset_url.split("token=")[1]
        reset = client.post(
            f"{API}/auth/reset-password", json={"token": token, "new_password": "BrandNew123"}
        )
        assert reset.status_code == 200

        assert (
            client.post(
                f"{API}/auth/login", json={"email": email, "password": "BrandNew123"}
            ).status_code
            == 200
        )
        # Single-use token.
        assert (
            client.post(
                f"{API}/auth/reset-password", json={"token": token, "new_password": "Another123"}
            ).status_code
            == 400
        )

    def test_forgot_password_does_not_leak_unknown_accounts(self, client: TestClient) -> None:
        response = client.post(f"{API}/auth/forgot-password", json={"email": "nobody@example.com"})
        assert response.status_code == 200
        assert response.json()["reset_url"] is None


class TestUserAdministration:
    """The /users endpoints, which are the only way to assign a role."""

    def test_listing_requires_an_administrator(self, auth_client: TestClient) -> None:
        assert auth_client.get(f"{API}/users").status_code == 403

    def test_anonymous_access_is_rejected(self, client: TestClient) -> None:
        assert client.get(f"{API}/users").status_code == 401

    def test_admin_can_create_a_user_with_a_role(self, admin_client: TestClient) -> None:
        response = admin_client.post(
            f"{API}/users",
            json={
                "email": "clerk@example.com",
                "full_name": "Church Clerk",
                "password": "StrongPass123",
                "role": "admin",
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["role"] == "admin"
        assert body["is_active"] is True
        # The password hash must never leave the server.
        assert "hashed_password" not in body
        assert "password" not in body

    def test_created_user_can_sign_in_but_registration_response_is_not_returned(
        self, admin_client: TestClient
    ) -> None:
        email = unique_email()
        admin_client.post(
            f"{API}/users",
            json={
                "email": email,
                "full_name": "New Clerk",
                "password": "StrongPass123",
                "role": "secretary",
            },
        )
        # No tokens are issued, so the administrator has to hand the password over.
        login = admin_client.post(
            f"{API}/auth/login", json={"email": email, "password": "StrongPass123"}
        )
        assert login.status_code == 200
        assert login.json()["user"]["role"] == "secretary"

    def test_duplicate_email_is_rejected(self, admin_client: TestClient) -> None:
        payload = {
            "email": "clerk@example.com",
            "full_name": "Clerk",
            "password": "StrongPass123",
        }
        assert admin_client.post(f"{API}/users", json=payload).status_code == 201
        assert admin_client.post(f"{API}/users", json=payload).status_code == 409

    def test_role_label_reflects_the_account(self, admin_client: TestClient) -> None:
        """The label is derived from the stored role, and inactive wins over it."""
        users = admin_client.get(f"{API}/users").json()
        roles = {user["email"]: user["role"] for user in users}
        assert roles["admin@example.com"] == "admin"

        created = admin_client.post(
            f"{API}/users",
            json={
                "email": "clerk@example.com",
                "full_name": "Clerk",
                "password": "StrongPass123",
                "role": "secretary",
            },
        ).json()
        assert created["role"] == "secretary"
        assert created["role_label"] == "Secretary"

        # No role chosen means the least privileged one, never admin.
        defaulted = admin_client.post(
            f"{API}/users",
            json={"email": unique_email(), "full_name": "Default", "password": "StrongPass123"},
        ).json()
        assert defaulted["role"] == "accountant"

        deactivated = admin_client.patch(
            f"{API}/users/{created['id']}", json={"is_active": False}
        ).json()
        assert deactivated["role_label"] == "Deactivated"
        # The stored role survives deactivation, so reactivating restores it.
        assert deactivated["role"] == "secretary"

    def test_each_role_reports_its_own_permissions(self, admin_client: TestClient) -> None:
        """The client is told what to hide; the server still checks everything."""
        created = admin_client.post(
            f"{API}/users",
            json={
                "email": "auditor@example.com",
                "full_name": "Auditor",
                "password": "StrongPass123",
                "role": "accountant",
            },
        ).json()
        assert "money:out" in created["permissions"]
        assert "approvals:decide" in created["permissions"]
        assert "users:manage" not in created["permissions"]
        assert "audit:view" not in created["permissions"]

    def test_deactivated_user_is_locked_out_immediately(self, admin_client: TestClient) -> None:
        email = unique_email()
        created = admin_client.post(
            f"{API}/users",
            json={"email": email, "full_name": "Leaving", "password": "StrongPass123"},
        ).json()

        token = admin_client.post(
            f"{API}/auth/login", json={"email": email, "password": "StrongPass123"}
        ).json()["tokens"]["access_token"]

        assert (
            admin_client.patch(
                f"{API}/users/{created['id']}", json={"is_active": False}
            ).status_code
            == 200
        )

        # The already-issued access token dies on the next request, and signing in
        # again is refused too.
        assert (
            admin_client.get(
                f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 401
        )
        assert (
            admin_client.post(
                f"{API}/auth/login", json={"email": email, "password": "StrongPass123"}
            ).status_code
            == 403
        )

    def test_administrator_cannot_demote_themselves(self, admin_client: TestClient) -> None:
        """The rule that makes permanent lockout impossible.

        Reaching this endpoint already proves the caller is an active
        administrator, so refusing to strip their own access guarantees one
        always remains.
        """
        me = admin_client.get(f"{API}/auth/me").json()
        response = admin_client.patch(f"{API}/users/{me['id']}", json={"role": "secretary"})
        assert response.status_code == 400
        assert "your own" in response.json()["detail"].lower()

    def test_administrator_cannot_deactivate_themselves(self, admin_client: TestClient) -> None:
        me = admin_client.get(f"{API}/auth/me").json()
        response = admin_client.patch(f"{API}/users/{me['id']}", json={"is_active": False})
        assert response.status_code == 400
        assert "your own" in response.json()["detail"].lower()
        # Still signed in: the refused change did not take effect.
        assert admin_client.get(f"{API}/auth/me").status_code == 200

    def test_an_administrator_may_demote_another_administrator(
        self, admin_client: TestClient
    ) -> None:
        second = admin_client.post(
            f"{API}/users",
            json={
                "email": "second@example.com",
                "full_name": "Second Admin",
                "password": "StrongPass123",
                "role": "admin",
            },
        ).json()
        assert second["role"] == "admin"

        demoted = admin_client.patch(f"{API}/users/{second['id']}", json={"role": "secretary"})
        assert demoted.status_code == 200
        assert demoted.json()["role"] == "secretary"

    def test_administrator_may_deactivate_another_administrator(
        self, admin_client: TestClient
    ) -> None:
        second = admin_client.post(
            f"{API}/users",
            json={
                "email": "second@example.com",
                "full_name": "Second Admin",
                "password": "StrongPass123",
                "role": "admin",
            },
        ).json()
        assert (
            admin_client.patch(f"{API}/users/{second['id']}", json={"is_active": False}).status_code
            == 200
        )

    def test_a_demoted_administrator_loses_admin_access(self, admin_client: TestClient) -> None:
        """Proves the role is enforced per request, not just shown in the response."""
        email = unique_email()
        second = admin_client.post(
            f"{API}/users",
            json={
                "email": email,
                "full_name": "Second Admin",
                "password": "StrongPass123",
                "role": "admin",
            },
        ).json()

        token = admin_client.post(
            f"{API}/auth/login", json={"email": email, "password": "StrongPass123"}
        ).json()["tokens"]["access_token"]
        assert (
            admin_client.get(
                f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 200
        )
        assert (
            admin_client.get(
                f"{API}/users", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 200
        )

        assert (
            admin_client.patch(
                f"{API}/users/{second['id']}", json={"role": "secretary"}
            ).status_code
            == 200
        )

        # The same token is now rejected: no caching of the old role.
        assert (
            admin_client.get(
                f"{API}/users", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 403
        )

    def test_a_plain_user_can_still_be_deactivated(self, admin_client: TestClient) -> None:
        created = admin_client.post(
            f"{API}/users",
            json={
                "email": "plain@example.com",
                "full_name": "Plain",
                "password": "StrongPass123",
            },
        ).json()
        assert (
            admin_client.patch(
                f"{API}/users/{created['id']}", json={"is_active": False}
            ).status_code
            == 200
        )

    def test_full_name_can_be_edited(self, admin_client: TestClient) -> None:
        created = admin_client.post(
            f"{API}/users",
            json={
                "email": "plain@example.com",
                "full_name": "Plain",
                "password": "StrongPass123",
            },
        ).json()
        response = admin_client.patch(
            f"{API}/users/{created['id']}", json={"full_name": "  Renamed Person  "}
        )
        assert response.status_code == 200
        assert response.json()["full_name"] == "Renamed Person"

    def test_blank_full_name_is_rejected(self, admin_client: TestClient) -> None:
        created = admin_client.post(
            f"{API}/users",
            json={
                "email": "plain@example.com",
                "full_name": "Plain",
                "password": "StrongPass123",
            },
        ).json()
        assert (
            admin_client.patch(
                f"{API}/users/{created['id']}", json={"full_name": "   "}
            ).status_code
            == 422
        )

    def test_admin_can_reset_a_password_and_old_sessions_die(
        self, admin_client: TestClient
    ) -> None:
        email = unique_email()
        created = admin_client.post(
            f"{API}/users",
            json={"email": email, "full_name": "Forgetful", "password": "StrongPass123"},
        ).json()

        refresh_token = admin_client.post(
            f"{API}/auth/login", json={"email": email, "password": "StrongPass123"}
        ).json()["tokens"]["refresh_token"]

        response = admin_client.post(
            f"{API}/users/{created['id']}/password", json={"new_password": "ResetPass456"}
        )
        assert response.status_code == 200
        assert email in response.json()["message"]

        # The session open before the reset no longer refreshes.
        assert (
            admin_client.post(
                f"{API}/auth/refresh", json={"refresh_token": refresh_token}
            ).status_code
            == 401
        )
        assert (
            admin_client.post(
                f"{API}/auth/login", json={"email": email, "password": "ResetPass456"}
            ).status_code
            == 200
        )
        assert (
            admin_client.post(
                f"{API}/auth/login", json={"email": email, "password": "StrongPass123"}
            ).status_code
            == 401
        )

    def test_admin_can_sign_a_user_out_everywhere(self, admin_client: TestClient) -> None:
        email = unique_email()
        created = admin_client.post(
            f"{API}/users",
            json={"email": email, "full_name": "Busy", "password": "StrongPass123"},
        ).json()
        session = admin_client.post(
            f"{API}/auth/login", json={"email": email, "password": "StrongPass123"}
        ).json()["tokens"]

        assert admin_client.post(f"{API}/users/{created['id']}/sessions").status_code == 200
        assert (
            admin_client.post(
                f"{API}/auth/refresh", json={"refresh_token": session["refresh_token"]}
            ).status_code
            == 401
        )

    def test_weak_password_is_rejected_on_provision(self, admin_client: TestClient) -> None:
        response = admin_client.post(
            f"{API}/users",
            json={"email": unique_email(), "full_name": "Weak", "password": "abc"},
        )
        assert response.status_code == 422
        assert "at least 8" in response.json()["detail"].lower()

    def test_unknown_user_id_is_a_404(self, admin_client: TestClient) -> None:
        missing = "00000000-0000-0000-0000-000000000000"
        assert (
            admin_client.patch(f"{API}/users/{missing}", json={"is_active": False}).status_code
            == 404
        )
        assert admin_client.post(f"{API}/users/{missing}/sessions").status_code == 404
        assert (
            admin_client.post(
                f"{API}/users/{missing}/password", json={"new_password": "StrongPass123"}
            ).status_code
            == 404
        )


class TestTransactions:
    def test_create_list_and_delete(self, auth_client: TestClient) -> None:
        payload = {
            "type": "income",
            "category": "Tithes",
            "party": "John Doe",
            "amount": "4500.50",
            "fund": "General Fund",
            "account": "M-PESA",
            "notes": "Sunday service",
            "date": date.today().isoformat(),
        }
        created = auth_client.post(f"{API}/transactions", json=payload)
        assert created.status_code == 201, created.text
        assert created.json()["amount"] == "4500.50"

        listed = auth_client.get(f"{API}/transactions", params={"type": "income"})
        assert listed.status_code == 200
        assert any(row["party"] == "John Doe" for row in listed.json())

        deleted = auth_client.delete(f"{API}/transactions/{created.json()['id']}")
        assert deleted.status_code == 200
        assert auth_client.get(f"{API}/transactions").json() == [] or all(
            row["id"] != created.json()["id"]
            for row in auth_client.get(f"{API}/transactions").json()
        )

    def test_income_cannot_use_an_expense_category(self, auth_client: TestClient) -> None:
        response = auth_client.post(
            f"{API}/transactions",
            json={
                "type": "income",
                "category": "Expenses",
                "amount": "100",
                "account": "Cash",
                "date": date.today().isoformat(),
            },
        )
        assert response.status_code == 422

    def test_amount_must_be_positive(self, auth_client: TestClient) -> None:
        response = auth_client.post(
            f"{API}/transactions",
            json={
                "type": "income",
                "category": "Tithes",
                "amount": "-10",
                "account": "Cash",
                "date": date.today().isoformat(),
            },
        )
        assert response.status_code == 422

    def test_transfer_creates_two_paired_rows(self, auth_client: TestClient) -> None:
        response = auth_client.post(
            f"{API}/transactions/transfers",
            json={
                "from_account": "Bank",
                "to_account": "Cash",
                "amount": "20000",
                "date": date.today().isoformat(),
                "notes": "Cash withdrawal",
            },
        )
        assert response.status_code == 201, response.text
        transfer_id = response.json()["transfer_id"]

        transfers = auth_client.get(f"{API}/transactions/transfers").json()
        assert any(t["transfer_id"] == transfer_id for t in transfers)
        pair = next(t for t in transfers if t["transfer_id"] == transfer_id)
        assert (pair["from_account"], pair["to_account"]) == ("Bank", "Cash")

        rows = auth_client.get(f"{API}/transactions", params={"category": TRANSFER_CATEGORY}).json()
        assert len([r for r in rows if r["transfer_id"] == transfer_id]) == 2

    def test_transfer_rejects_identical_accounts(self, auth_client: TestClient) -> None:
        response = auth_client.post(
            f"{API}/transactions/transfers",
            json={
                "from_account": "Cash",
                "to_account": "Cash",
                "amount": "100",
                "date": date.today().isoformat(),
            },
        )
        assert response.status_code == 422

    def test_deleting_one_leg_removes_the_whole_transfer(self, auth_client: TestClient) -> None:
        created = auth_client.post(
            f"{API}/transactions/transfers",
            json={
                "from_account": "Bank",
                "to_account": "M-PESA",
                "amount": "5000",
                "date": date.today().isoformat(),
            },
        ).json()
        rows = auth_client.get(f"{API}/transactions", params={"category": TRANSFER_CATEGORY}).json()
        leg = next(r for r in rows if r["transfer_id"] == created["transfer_id"])

        assert auth_client.delete(f"{API}/transactions/{leg['id']}").status_code == 200
        remaining = auth_client.get(
            f"{API}/transactions", params={"category": TRANSFER_CATEGORY}
        ).json()
        assert all(r["transfer_id"] != created["transfer_id"] for r in remaining)

    def test_filters(self, auth_client: TestClient) -> None:
        auth_client.post(
            f"{API}/transactions",
            json={
                "type": "expense",
                "category": "Petty Cash",
                "party": "Office Supplies Ltd",
                "amount": "1200",
                "fund": "Youth",
                "account": "Cash",
                "date": date.today().isoformat(),
                "notes": "Stationery",
            },
        )
        filtered = auth_client.get(
            f"{API}/transactions", params={"type": "expense", "account": "Cash", "fund": "Youth"}
        ).json()
        assert all(
            row["type"] == "expense" and row["account"] == "Cash" and row["fund"] == "Youth"
            for row in filtered
        )
        searched = auth_client.get(f"{API}/transactions", params={"search": "Stationery"}).json()
        assert any(row["notes"] == "Stationery" for row in searched)


class TestPeople:
    def test_crud_cycle(self, secretary_client: IdentityClient) -> None:
        created = secretary_client.post(
            f"{API}/people",
            json={
                "role": "Member",
                "name": "  Grace   Wanjiru ",
                "phone": "+254700000000",
                "category": "Women",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["name"] == "Grace Wanjiru"

        person_id = created.json()["id"]
        updated = secretary_client.patch(
            f"{API}/people/{person_id}", json={"phone": "+254711111111", "notes": "Baptised"}
        )
        assert updated.status_code == 200
        assert updated.json()["phone"] == "+254711111111"

        filtered = secretary_client.get(f"{API}/people", params={"role": "Member"}).json()
        assert any(p["id"] == person_id for p in filtered)

        searched = secretary_client.get(f"{API}/people", params={"search": "wanjiru"}).json()
        assert any(p["id"] == person_id for p in searched)

        counts = secretary_client.get(f"{API}/people/count").json()
        assert counts["Member"] >= 1
        assert counts["total"] >= 1

        assert secretary_client.delete(f"{API}/people/{person_id}").status_code == 200
        assert (
            secretary_client.patch(f"{API}/people/{person_id}", json={"phone": "x"}).status_code
            == 404
        )

    def test_member_category_is_validated(self, secretary_client: IdentityClient) -> None:
        response = secretary_client.post(
            f"{API}/people", json={"role": "Member", "name": "Bad Category", "category": "Aliens"}
        )
        assert response.status_code == 422

    def test_unknown_role_is_rejected(self, secretary_client: IdentityClient) -> None:
        assert (
            secretary_client.post(
                f"{API}/people", json={"role": "Bishop", "name": "Nope"}
            ).status_code
            == 422
        )


class TestReporting:
    def test_dashboard_totals_reflect_recorded_entries(self, auth_client: TestClient) -> None:
        today = date.today().isoformat()
        auth_client.post(
            f"{API}/transactions",
            json={
                "type": "income",
                "category": "Tithes",
                "party": "Peter Mwangi",
                "amount": "10000",
                "fund": "Building",
                "account": "Bank",
                "date": today,
            },
        )
        auth_client.post(
            f"{API}/transactions",
            json={
                "type": "expense",
                "category": "Expenses",
                "amount": "2500",
                "fund": "Building",
                "account": "Bank",
                "date": today,
            },
        )

        dashboard = auth_client.get(f"{API}/dashboard").json()
        assert Decimal(dashboard["total_income"]) >= Decimal("10000")
        assert Decimal(dashboard["total_expenses"]) >= Decimal("2500")
        assert dashboard["net_balance"]
        assert any(row["key"] == "Bank" for row in dashboard["balance_by_account"])
        assert any(row["key"] == "Building" for row in dashboard["net_by_fund"])
        assert len(dashboard["recent_transactions"]) <= 8

    def test_trial_balance_stays_balanced(self, auth_client: TestClient) -> None:
        trial = auth_client.get(f"{API}/accounting/trial-balance").json()
        assert trial["is_balanced"] is True
        assert trial["difference"] == "0.00"
        assert trial["total_debits"] == trial["total_credits"]

    def test_chart_of_accounts_and_statements(self, auth_client: TestClient) -> None:
        chart = auth_client.get(f"{API}/accounting/chart-of-accounts").json()
        assert chart["is_balanced"] is True
        assert all(
            {"account", "account_type", "debit", "credit", "balance"} <= set(row)
            for row in chart["accounts"]
        )

        statements = auth_client.get(f"{API}/accounting/financial-statements").json()
        assert {"income", "expenses", "net_surplus", "account_position", "total_assets"} <= set(
            statements
        )

    def test_member_contribution_history(self, auth_client: TestClient) -> None:
        today = date.today().isoformat()
        for amount, category in (("3000", "Tithes"), ("1500", "Offerings")):
            auth_client.post(
                f"{API}/transactions",
                json={
                    "type": "income",
                    "category": category,
                    "party": "Samuel Kiptoo",
                    "amount": amount,
                    "account": "Cash",
                    "date": today,
                },
            )

        names = auth_client.get(f"{API}/reports/member-contributions").json()
        assert "Samuel Kiptoo" in names

        detail = auth_client.get(
            f"{API}/reports/member-contributions/detail", params={"name": " samuel kiptoo "}
        ).json()
        assert len(detail) == 1
        assert detail[0]["total"] == "4500.00"
        assert detail[0]["contribution_count"] == 2
        assert {row["category"] for row in detail[0]["by_category"]} == {"Tithes", "Offerings"}

    def test_ledger_and_reference_data(self, auth_client: TestClient) -> None:
        assert auth_client.get(f"{API}/accounting/ledger").status_code == 200
        reference = auth_client.get(f"{API}/reference").json()
        assert reference["accounts"] == ["Cash", "Bank", "M-PESA"]
        assert reference["transfer_category"] == "Transfer"
        assert "Tithes" in reference["income_categories"]
        assert reference["person_roles"] == [
            "Member",
            "Guest",
            "Supplier",
            "Employee",
            "User",
        ]

    def test_summary_report(self, auth_client: TestClient) -> None:
        report = auth_client.get(f"{API}/reports/summary").json()
        assert {row["category"] for row in report["income_by_category"]} >= {"Tithes", "Offerings"}


class TestPercentageDeduction:
    """The Tithe % Deduction page: deduct a slice of one Sunday's tithe collection.

    The account in the form picks which collection is read and where the money
    leaves from; ``all`` covers every account that held tithes that day.
    """

    SUNDAY = date(2026, 3, 8).isoformat()

    def _record_tithes(self, client: TestClient, *amounts: str, account: str = "Bank") -> None:
        for amount in amounts:
            response = client.post(
                f"{API}/transactions",
                json={
                    "type": "income",
                    "category": "Tithes",
                    "party": "Mary Achieng",
                    "amount": amount,
                    "fund": "General Fund",
                    "account": account,
                    "date": self.SUNDAY,
                },
            )
            assert response.status_code == 201, response.text

    def test_tithes_on_date_sums_only_that_day(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        self._record_tithes(auth_client, "3000.50", "1999.50")
        auth_client.post(
            f"{API}/transactions",
            json={
                "type": "income",
                "category": "Offerings",
                "amount": "500",
                "account": "Bank",
                "date": self.SUNDAY,
            },
        )

        response = auth_client.get(f"{API}/accounting/tithes-on-date", params={"date": self.SUNDAY})
        found = response.json()
        # Two tithe entries totalling 5,000; the 500 of Offerings is not counted.
        assert Decimal(found["total"]) == Decimal("5000.00")
        assert found["entry_count"] == 2

    def test_deduction_creates_one_expense_and_reduces_the_account(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        self._record_tithes(auth_client, "3000", "1000")
        before = Decimal(
            next(
                row["total"]
                for row in auth_client.get(f"{API}/transactions/accounts/balances").json()
                if row["key"] == "Bank"
            )
        )

        response = auth_client.post(
            f"{API}/transactions/pct-deduction",
            json={
                "date": self.SUNDAY,
                "pct": "10",
                "account": "Bank",
                "notes": "Diocese remittance",
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert Decimal(body["deduction_amount"]) == Decimal("400.00")
        assert Decimal(body["tithes_that_day"]) == Decimal("4000.00")

        assert len(body["transactions"]) == 1
        entry = body["transactions"][0]
        assert entry["type"] == "expense"
        assert entry["category"] == "Percentage Deduction"
        assert entry["account"] == "Bank"
        assert entry["fund"] == "General Fund"
        assert entry["party"] == "Diocese remittance"
        assert entry["pct"] == "10.00"
        assert entry["base_total"] == "4000.00"

        after = Decimal(
            next(
                row["total"]
                for row in auth_client.get(f"{API}/transactions/accounts/balances").json()
                if row["key"] == "Bank"
            )
        )
        assert before - after == Decimal("400.00")

    def test_deduction_appears_in_history_and_money_out(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        self._record_tithes(auth_client, "5000")
        auth_client.post(
            f"{API}/transactions/pct-deduction",
            json={"date": self.SUNDAY, "pct": "12.5", "account": "Bank"},
        )

        history = auth_client.get(f"{API}/transactions/pct-deductions").json()
        assert len(history) == 1
        assert history[0]["pct"] == "12.50"
        # No purpose typed in, so the category doubles as the counterparty.
        assert history[0]["party"] == "Percentage Deduction"

        money_out = auth_client.get(
            f"{API}/transactions", params={"type": "expense", "category": "Percentage Deduction"}
        ).json()
        assert len(money_out) == 1

        report = auth_client.get(f"{API}/reports/summary").json()
        assert any(
            row["category"] == "Percentage Deduction" and Decimal(row["total"]) == Decimal("625.00")
            for row in report["expense_by_category"]
        )

    def test_deduction_is_rejected_with_no_tithes_that_day(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        response = auth_client.post(
            f"{API}/transactions/pct-deduction",
            json={"date": "2026-03-01", "pct": "10", "account": "Cash"},
        )
        assert response.status_code == 422
        assert "nothing to deduct" in response.json()["detail"]

    def test_choosing_an_account_with_no_tithes_is_refused(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        # The tithes went into the Bank, so asking for Cash has nothing to work on.
        self._record_tithes(auth_client, "4000")
        response = auth_client.post(
            f"{API}/transactions/pct-deduction",
            json={"date": self.SUNDAY, "pct": "10", "account": "Cash"},
        )
        assert response.status_code == 422
        assert "No Tithes were collected in Cash" in response.json()["detail"]

    def test_zero_and_over_100_percentages_are_rejected(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        self._record_tithes(auth_client, "2000")
        for pct in ("0", "101"):
            response = auth_client.post(
                f"{API}/transactions/pct-deduction",
                json={"date": self.SUNDAY, "pct": pct, "account": "Cash"},
            )
            assert response.status_code == 422, f"pct={pct} was accepted"

    def test_a_tiny_percentage_rounding_to_zero_is_refused(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        # 1.00 at 0.1% is 0.001, which rounds to 0.00 - there is no entry to post.
        self._record_tithes(auth_client, "1")
        response = auth_client.post(
            f"{API}/transactions/pct-deduction",
            json={"date": self.SUNDAY, "pct": "0.1", "account": "Bank"},
        )
        assert response.status_code == 422
        assert "KSh 0" in response.json()["detail"]

    def test_preview_matches_what_gets_recorded(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        self._record_tithes(auth_client, "8333.33")
        preview = auth_client.get(
            f"{API}/accounting/pct-deduction-preview",
            params={"date": self.SUNDAY, "pct": "7.5", "account": "Bank"},
        ).json()
        assert Decimal(preview["tithes_that_day"]) == Decimal("8333.33")
        assert Decimal(preview["deduction_amount"]) == Decimal("625.00")

    def test_pct_cannot_be_set_on_an_ordinary_expense(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        response = auth_client.post(
            f"{API}/transactions",
            json={
                "type": "expense",
                "category": "Expenses",
                "amount": "100",
                "account": "Cash",
                "date": self.SUNDAY,
                "pct": "10",
                "base_total": "1000",
            },
        )
        assert response.status_code == 422
        assert "Percentage Deduction" in response.json()["detail"]

    def test_deductions_never_unbalance_the_books(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        self._record_tithes(auth_client, "20000")
        auth_client.post(
            f"{API}/transactions/pct-deduction",
            json={"date": self.SUNDAY, "pct": "10", "account": "Bank"},
        )
        trial = auth_client.get(f"{API}/accounting/trial-balance").json()
        assert trial["is_balanced"] is True
        assert trial["difference"] == "0.00"

    # ------------------------------------------------------- account scope --

    def _balances(self, client: TestClient) -> dict[str, Decimal]:
        return {
            row["key"]: Decimal(row["total"])
            for row in client.get(f"{API}/transactions/accounts/balances").json()
        }

    def _record_income(self, client: TestClient, category: str, amount: str) -> None:
        response = client.post(
            f"{API}/transactions",
            json={
                "type": "income",
                "category": category,
                "amount": amount,
                "account": "Bank",
                "date": self.SUNDAY,
            },
        )
        assert response.status_code == 201, response.text

    def test_the_account_picks_which_collection_is_read(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        self._record_tithes(auth_client, "4000")
        self._record_tithes(auth_client, "1000", account="Cash")
        self._record_tithes(auth_client, "500", account="M-PESA")

        response = auth_client.post(
            f"{API}/transactions/pct-deduction",
            json={"date": self.SUNDAY, "pct": "10", "account": "Bank"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        # Only the Bank's tithes; the 1,500 sitting in Cash and M-PESA is ignored.
        assert Decimal(body["tithes_that_day"]) == Decimal("4000.00")
        assert Decimal(body["deduction_amount"]) == Decimal("400.00")
        assert [row["account"] for row in body["transactions"]] == ["Bank"]

    def test_only_the_tithes_category_is_ever_read(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        self._record_tithes(auth_client, "4000")
        self._record_income(auth_client, "Offerings", "2000")

        response = auth_client.post(
            f"{API}/transactions/pct-deduction",
            json={"date": self.SUNDAY, "pct": "10", "account": "all"},
        )
        assert response.status_code == 201, response.text
        assert Decimal(response.json()["tithes_that_day"]) == Decimal("4000.00")

    def test_all_covers_every_account_and_splits_the_deduction(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        self._record_tithes(auth_client, "4000")
        self._record_tithes(auth_client, "3000", account="Cash")
        self._record_tithes(auth_client, "1000", account="M-PESA")
        before = self._balances(auth_client)

        response = auth_client.post(
            f"{API}/transactions/pct-deduction",
            json={"date": self.SUNDAY, "pct": "10", "account": "all"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert Decimal(body["tithes_that_day"]) == Decimal("8000.00")
        assert Decimal(body["deduction_amount"]) == Decimal("800.00")
        assert {share["account"]: share["deduction_amount"] for share in body["shares"]} == {
            "Bank": "400.00",
            "Cash": "300.00",
            "M-PESA": "100.00",
        }

        # One entry per account, each for its own share, so no balance pays for
        # tithes collected somewhere else.
        assert sorted(row["account"] for row in body["transactions"]) == [
            "Bank",
            "Cash",
            "M-PESA",
        ]
        assert {row["account"]: row["base_total"] for row in body["transactions"]} == {
            "Bank": "4000.00",
            "Cash": "3000.00",
            "M-PESA": "1000.00",
        }
        after = self._balances(auth_client)
        assert before["Bank"] - after["Bank"] == Decimal("400.00")
        assert before["Cash"] - after["Cash"] == Decimal("300.00")
        assert before["M-PESA"] - after["M-PESA"] == Decimal("100.00")

    def test_all_with_one_account_holding_tithes_posts_one_entry(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        self._record_tithes(auth_client, "4000")
        response = auth_client.post(
            f"{API}/transactions/pct-deduction",
            json={"date": self.SUNDAY, "pct": "10", "account": "all"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert [row["account"] for row in body["transactions"]] == ["Bank"]
        assert Decimal(body["deduction_amount"]) == Decimal("400.00")

    def test_all_is_refused_when_no_account_held_tithes(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        response = auth_client.post(
            f"{API}/transactions/pct-deduction",
            json={"date": "2026-03-01", "pct": "10", "account": "all"},
        )
        assert response.status_code == 422
        assert "any account" in response.json()["detail"]

    def test_a_transfer_is_not_treated_as_collected_tithes(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        # Moving money from the Bank into Cash that day must not make Cash look
        # like it collected anything.
        self._record_tithes(auth_client, "4000")
        response = auth_client.post(
            f"{API}/transactions/transfers",
            json={
                "from_account": "Bank",
                "to_account": "Cash",
                "amount": "2500",
                "date": self.SUNDAY,
            },
        )
        assert response.status_code == 201, response.text

        refused = auth_client.post(
            f"{API}/transactions/pct-deduction",
            json={"date": self.SUNDAY, "pct": "10", "account": "Cash"},
        )
        assert refused.status_code == 422

        accepted = auth_client.post(
            f"{API}/transactions/pct-deduction",
            json={"date": self.SUNDAY, "pct": "10", "account": "all"},
        )
        assert accepted.status_code == 201, accepted.text
        assert Decimal(accepted.json()["tithes_that_day"]) == Decimal("4000.00")

    def test_preview_follows_the_chosen_account(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        self._record_tithes(auth_client, "4000")
        self._record_tithes(auth_client, "1000", account="Cash")

        def preview(**params: str) -> dict[str, object]:
            response = auth_client.get(
                f"{API}/accounting/pct-deduction-preview",
                params={"date": self.SUNDAY, "pct": "10", **params},
            )
            assert response.status_code == 200, response.text
            return response.json()

        bank = preview(account="Bank")
        assert Decimal(bank["tithes_that_day"]) == Decimal("4000.00")  # type: ignore[arg-type]
        assert Decimal(bank["deduction_amount"]) == Decimal("400.00")  # type: ignore[arg-type]

        cash = preview(account="Cash")
        assert Decimal(cash["tithes_that_day"]) == Decimal("1000.00")  # type: ignore[arg-type]
        assert Decimal(cash["deduction_amount"]) == Decimal("100.00")  # type: ignore[arg-type]

        # No account given falls back to Cash, the first row of the dropdown.
        default = preview()
        assert Decimal(default["tithes_that_day"]) == Decimal("1000.00")  # type: ignore[arg-type]

        everything = preview(account="all")
        assert Decimal(everything["tithes_that_day"]) == Decimal("5000.00")  # type: ignore[arg-type]
        assert Decimal(everything["deduction_amount"]) == Decimal("500.00")  # type: ignore[arg-type]
        assert {share["account"] for share in everything["shares"]} == {"Bank", "Cash"}  # type: ignore[union-attr]

    def test_the_scope_allows_the_accounts_and_all_only(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        self._record_tithes(auth_client, "4000")
        for account in ("Cash", "Bank", "M-PESA", "all"):
            accepted = auth_client.get(
                f"{API}/accounting/pct-deduction-preview",
                params={"date": self.SUNDAY, "pct": "10", "account": account},
            )
            assert accepted.status_code == 200, f"{account} was refused"

        for account in ("Cheque", "ALL", "Petty Cash", ""):
            refused = auth_client.post(
                f"{API}/transactions/pct-deduction",
                json={"date": self.SUNDAY, "pct": "10", "account": account},
            )
            assert refused.status_code == 422, f"{account!r} was accepted"

    def test_the_scope_constant_and_the_schema_literal_agree(self) -> None:
        # mypy will not take a variable inside Literal[], so TitheScope spells the
        # "all" literal out by hand. This pins the copy to the constant the route
        # compares against, so the two cannot drift apart unnoticed.
        assert TITHE_SCOPE_ALL == "all"
        payload = PctDeductionCreate(
            date=date(2026, 3, 8),
            pct=Decimal("10"),
            account=TITHE_SCOPE_ALL,
        )
        assert payload.account == "all"
        assert (
            PctDeductionCreate(date=date(2026, 3, 8), pct=Decimal("10"), account="Cash").account
            is Account.CASH
        )


class TestContributionSearch:
    """Reports page search: partial, case-insensitive name matching."""

    def _give(self, client: TestClient, party: str, amount: str, category: str = "Tithes") -> None:
        response = client.post(
            f"{API}/transactions",
            json={
                "type": "income",
                "category": category,
                "party": party,
                "amount": amount,
                "account": "Cash",
                "date": date.today().isoformat(),
            },
        )
        assert response.status_code == 201, response.text

    def test_partial_name_matches(self, auth_client: TestClient, clean_db: None) -> None:
        self._give(auth_client, "John Otieno", "5000")
        self._give(auth_client, "Mary Atieno Otieno", "1500")
        self._give(auth_client, "Peter Mwangi", "2000")

        found = auth_client.get(f"{API}/reports/contributions", params={"search": "otieno"}).json()
        assert found["count"] == 2
        assert Decimal(found["total"]) == Decimal("6500.00")
        assert {row["party"] for row in found["transactions"]} == {
            "John Otieno",
            "Mary Atieno Otieno",
        }

    def test_search_is_case_and_whitespace_insensitive(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        self._give(auth_client, "Samuel Kiptoo", "3000")
        found = auth_client.get(
            f"{API}/reports/contributions", params={"search": "  SAMUEL  "}
        ).json()
        assert found["count"] == 1
        assert found["transactions"][0]["party"] == "Samuel Kiptoo"

    def test_no_matches_is_an_empty_result_not_an_error(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        found = auth_client.get(f"{API}/reports/contributions", params={"search": "zzz"}).json()
        assert found["count"] == 0
        assert found["total"] == "0.00"
        assert found["transactions"] == []

    def test_expenses_and_transfers_are_not_contributions(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        self._give(auth_client, "Nia Odera", "1000")
        auth_client.post(
            f"{API}/transactions",
            json={
                "type": "expense",
                "category": "Suppliers",
                "party": "Nia Odera Supplies",
                "amount": "700",
                "account": "Cash",
                "date": date.today().isoformat(),
            },
        )
        auth_client.post(
            f"{API}/transactions/transfers",
            json={
                "from_account": "Bank",
                "to_account": "Cash",
                "amount": "4000",
                "date": date.today().isoformat(),
            },
        )

        found = auth_client.get(f"{API}/reports/contributions", params={"search": "nia"}).json()
        assert found["count"] == 1
        assert found["transactions"][0]["party"] == "Nia Odera"


class TestFundSummary:
    """The per-fund screen needs received, spent and net from the server.

    Summing a capped page of rows instead would under-report once a fund passes
    the page size, so these figures are aggregated in SQL.
    """

    def test_reports_received_spent_and_net_for_every_fund(
        self, auth_client: TestClient, clean_db: None
    ) -> None:
        for payload in (
            {"type": "income", "category": "Tithes", "amount": "1000", "fund": "Building"},
            {"type": "expense", "category": "Suppliers", "amount": "250", "fund": "Building"},
            {"type": "income", "category": "Offerings", "amount": "500", "fund": "Missions"},
        ):
            response = auth_client.post(
                f"{API}/transactions",
                json={
                    **payload,
                    "account": "Cash",
                    "date": date.today().isoformat(),
                },
            )
            assert response.status_code == 201, response.text

        rows = auth_client.get(f"{API}/transactions/funds/summary").json()
        by_fund = {row["fund"]: row for row in rows}

        assert by_fund["Building"]["received"] == "1000.00"
        assert by_fund["Building"]["spent"] == "250.00"
        assert by_fund["Building"]["net"] == "750.00"
        assert by_fund["Missions"]["received"] == "500.00"
        assert by_fund["Missions"]["spent"] == "0.00"
        assert by_fund["Missions"]["net"] == "500.00"
        # Every fund is listed, so the screen never has to guess a missing row.
        assert by_fund["General Fund"] == {
            "fund": "General Fund",
            "received": "0.00",
            "spent": "0.00",
            "net": "0.00",
        }

    def test_excludes_transfer_legs(self, auth_client: TestClient, clean_db: None) -> None:
        """A transfer moves money without income or expense, so it nets to zero."""
        auth_client.post(
            f"{API}/transactions/transfers",
            json={
                "from_account": "Bank",
                "to_account": "Cash",
                "amount": "4000",
                "date": date.today().isoformat(),
            },
        )
        rows = {
            row["fund"]: row for row in auth_client.get(f"{API}/transactions/funds/summary").json()
        }
        for row in rows.values():
            assert Decimal(row["net"]) == Decimal("0.00"), row

    def test_requires_a_token(self, client: TestClient) -> None:
        assert client.get(f"{API}/transactions/funds/summary").status_code == 401


class TestSummaryFixtures:
    def test_configured_test_user_exists(self, auth_client: TestClient) -> None:
        me = auth_client.get(f"{API}/auth/me").json()
        assert me["email"] == TEST_USER["email"]

    def test_reference_data_requires_a_token(self, client: TestClient) -> None:
        assert client.get(f"{API}/reference").status_code == 401

    def test_openapi_document_is_complete(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        paths = schema["paths"]
        for expected in (
            "/api/v1/auth/login",
            "/api/v1/auth/refresh",
            "/api/v1/transactions",
            "/api/v1/transactions/transfers",
            "/api/v1/transactions/pct-deduction",
            "/api/v1/transactions/pct-deductions",
            "/api/v1/transactions/funds/summary",
            "/api/v1/accounting/tithes-on-date",
            "/api/v1/people",
            "/api/v1/dashboard",
            "/api/v1/accounting/trial-balance",
            "/api/v1/reports/contributions",
            "/api/v1/reference",
        ):
            assert expected in paths, f"{expected} is missing from the OpenAPI schema"
