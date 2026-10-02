"""Tests for the role matrix, the person email rules and the Guest role.

These live in their own file because the subject is the seam between roles: what a
secretary may do that an accountant may not, and what neither may do. The tables
in ``app/core/permissions.py`` are the specification; these tests are the
specification for the tables, so a role quietly gaining a power fails here.

Assertions are made against the API rather than the permission map on purpose. A
test that only read the map would still pass if a route forgot to ask for the
permission, which is the mistake worth catching.
"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from tests.conftest import IdentityClient

API = "/api/v1"

TODAY = date.today().isoformat()

TRANSFER = {
    "from_account": "Bank",
    "to_account": "Cash",
    "amount": "500.00",
    "date": TODAY,
    "notes": "Petty cash",
}

EXPENSE = {
    "type": "expense",
    "category": "Expenses",
    "party": "Kenya Power",
    "amount": "1200.00",
    "fund": "General Fund",
    "account": "Bank",
    "date": TODAY,
}

INCOME = {
    "type": "income",
    "category": "Tithes",
    "party": "Mary Achieng",
    "amount": "4000.00",
    "fund": "General Fund",
    "account": "Bank",
    "date": TODAY,
}

PERSON = {"role": "Member", "name": "Grace Wanjiru", "phone": "+254700000000"}


class TestRoleMatrix:
    """What each role can and cannot reach, endpoint by endpoint."""

    def test_a_secretary_can_record_money_in(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        response = secretary_client.post(f"{API}/transactions", json=INCOME)
        assert response.status_code == 201, response.text

    def test_a_secretary_cannot_record_money_out(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        response = secretary_client.post(f"{API}/transactions", json=EXPENSE)
        assert response.status_code == 403
        # Nothing was written, so no balance moved.
        assert secretary_client.get(f"{API}/transactions").json() == []

    def test_a_secretary_cannot_move_money_between_accounts(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        assert (
            secretary_client.post(f"{API}/transactions/transfers", json=TRANSFER).status_code == 403
        )

    def test_a_secretary_cannot_run_the_deduction(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        assert (
            secretary_client.get(
                f"{API}/accounting/tithes-on-date", params={"day": TODAY}
            ).status_code
            == 403
        )

    def test_a_secretary_cannot_read_the_accounting_reports(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        for path in (
            "/accounting/ledger",
            "/accounting/trial-balance",
            "/accounting/chart-of-accounts",
            "/accounting/financial-statements",
        ):
            assert secretary_client.get(f"{API}{path}").status_code == 403, path

    def test_a_secretary_can_read_the_raising_reports(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        """Read-only does not mean blind: the secretary still sees the totals."""
        assert secretary_client.get(f"{API}/dashboard").status_code == 200
        assert secretary_client.get(f"{API}/reports/summary").status_code == 200

    def test_a_secretary_keeps_the_directory(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        created = secretary_client.post(f"{API}/people", json=PERSON)
        assert created.status_code == 201, created.text

    def test_an_accountant_cannot_touch_the_directory(
        self, accountant_client: IdentityClient, clean_db: None
    ) -> None:
        """Reading it is fine - the Money In picker needs that - editing is not."""
        assert accountant_client.get(f"{API}/people").status_code == 200
        assert accountant_client.post(f"{API}/people", json=PERSON).status_code == 403

    def test_neither_money_role_may_manage_accounts_or_read_the_trail(
        self, accountant_client: IdentityClient, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        for client in (accountant_client, secretary_client):
            assert client.get(f"{API}/users").status_code == 403
            assert client.get(f"{API}/audit").status_code == 403

    def test_the_server_refuses_even_when_the_client_asks_nicely(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        """The 403 is the server's own answer, not something the interface prevents."""
        response = secretary_client.post(f"{API}/users", json={"email": "x@example.com"})
        assert response.status_code == 401 or response.status_code == 403

    def test_deleting_an_expense_needs_the_expense_permission(
        self, accountant_client: IdentityClient, clean_db: None
    ) -> None:
        """Deleting rewrites a balance, so it is gated on the same thing as writing."""
        created = accountant_client.post(f"{API}/transactions", json=EXPENSE).json()
        assert accountant_client.delete(f"{API}/transactions/{created['id']}").status_code == 200

    def test_a_secretary_may_correct_a_collection_but_not_an_expense(
        self, secretary_client: IdentityClient, accountant_client: IdentityClient, clean_db: None
    ) -> None:
        """Deletion is gated on the direction of the row, not on who made it.

        A mistyped collection is routine, so the secretary who records it can undo
        it; an expense is outside her role entirely. Every removal lands in the
        audit trail either way, with the actor's name on it.
        """
        income = accountant_client.post(f"{API}/transactions", json=INCOME).json()
        expense = accountant_client.post(f"{API}/transactions", json=EXPENSE).json()

        assert secretary_client.delete(f"{API}/transactions/{income['id']}").status_code == 200
        assert secretary_client.delete(f"{API}/transactions/{expense['id']}").status_code == 403

    def test_an_accountant_may_approve_but_a_secretary_may_not(
        self, accountant_client: IdentityClient, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        raised = secretary_client.post(
            f"{API}/approvals",
            json={
                "category": "Expenses",
                "party": "Kenya Power",
                "amount": "1500.00",
                "fund": "General Fund",
                "account": "Bank",
                "date": TODAY,
            },
        )
        assert raised.status_code == 201, raised.text
        approval_id = raised.json()["id"]

        assert (
            secretary_client.post(f"{API}/approvals/{approval_id}/approve", json={}).status_code
            == 403
        )
        approved = accountant_client.post(
            f"{API}/approvals/{approval_id}/approve", json={"note": "Checked"}
        )
        assert approved.status_code == 200, approved.text


class TestGuestRole:
    """A guest is a person who gives but is not on the membership roll."""

    def test_a_guest_can_be_added_and_counted_separately(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        created = secretary_client.post(
            f"{API}/people", json={"role": "Guest", "name": "John Visitor"}
        )
        assert created.status_code == 201, created.text

        counts = secretary_client.get(f"{API}/people/count").json()
        assert counts["Guest"] == 1
        # The point of the role: it does not inflate the membership count.
        assert counts["Member"] == 0

    def test_a_guest_does_not_need_an_email(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        created = secretary_client.post(
            f"{API}/people", json={"role": "Guest", "name": "No Address"}
        )
        assert created.status_code == 201, created.text
        assert created.json()["email"] == ""

    def test_the_guest_tab_appears_in_the_people_filter(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        secretary_client.post(f"{API}/people", json={"role": "Guest", "name": "Visitor One"})
        secretary_client.post(f"{API}/people", json={"role": "Member", "name": "Member One"})
        guests = secretary_client.get(f"{API}/people", params={"role": "Guest"}).json()
        assert [row["name"] for row in guests] == ["Visitor One"]


class TestPersonEmail:
    """An address is mandatory for the roles the church always emails."""

    def test_a_supplier_must_have_an_email(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        assert (
            secretary_client.post(
                f"{API}/people", json={"role": "Supplier", "name": "No Address Ltd"}
            ).status_code
            == 422
        )

    def test_an_employee_must_have_an_email(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        assert (
            secretary_client.post(
                f"{API}/people", json={"role": "Employee", "name": "Casual Cleaner"}
            ).status_code
            == 422
        )

    def test_a_member_does_not(self, secretary_client: IdentityClient, clean_db: None) -> None:
        assert (
            secretary_client.post(
                f"{API}/people", json={"role": "Member", "name": "No Address"}
            ).status_code
            == 201
        )

    def test_a_supplier_with_an_email_is_accepted(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        response = secretary_client.post(
            f"{API}/people",
            json={"role": "Supplier", "name": "Kenya Power", "email": "  billing@kp.co.ke  "},
        )
        assert response.status_code == 201, response.text
        # Trimmed on the way in, so searching for it does not need a fuzzy match.
        assert response.json()["email"] == "billing@kp.co.ke"

    def test_a_malformed_email_is_rejected(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        assert (
            secretary_client.post(
                f"{API}/people",
                json={"role": "Supplier", "name": "Bad Address", "email": "not-an-address"},
            ).status_code
            == 422
        )

    def test_promoting_a_member_to_supplier_demands_the_address(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        """The rule spans two fields, so a patch cannot be checked in isolation."""
        person = secretary_client.post(
            f"{API}/people", json={"role": "Member", "name": "Was A Member"}
        ).json()

        refused = secretary_client.patch(f"{API}/people/{person['id']}", json={"role": "Supplier"})
        assert refused.status_code == 422

        # Adding the address in the same request goes through.
        accepted = secretary_client.patch(
            f"{API}/people/{person['id']}",
            json={"role": "Supplier", "email": "now@kp.co.ke"},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["email"] == "now@kp.co.ke"

    def test_demoting_back_to_member_may_drop_the_email(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        person = secretary_client.post(
            f"{API}/people",
            json={"role": "Supplier", "name": "Kenya Power", "email": "billing@kp.co.ke"},
        ).json()

        response = secretary_client.patch(
            f"{API}/people/{person['id']}", json={"role": "Member", "email": ""}
        )
        assert response.status_code == 200, response.text
        assert response.json()["email"] == ""

    def test_clearing_the_email_on_a_supplier_alone_is_refused(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        person = secretary_client.post(
            f"{API}/people",
            json={"role": "Supplier", "name": "Kenya Power", "email": "billing@kp.co.ke"},
        ).json()
        assert (
            secretary_client.patch(f"{API}/people/{person['id']}", json={"email": ""}).status_code
            == 422
        )

    def test_search_finds_a_person_by_email(
        self, secretary_client: IdentityClient, clean_db: None
    ) -> None:
        secretary_client.post(
            f"{API}/people",
            json={"role": "Supplier", "name": "Kenya Power", "email": "billing@kp.co.ke"},
        )
        found = secretary_client.get(f"{API}/people", params={"search": "kp.co.ke"}).json()
        assert [row["name"] for row in found] == ["Kenya Power"]

    def test_the_email_reaches_the_audit_trail(
        self, secretary_client: IdentityClient, admin_client: TestClient, clean_db: None
    ) -> None:
        """It already did before the column existed - the snapshot asked for it."""
        secretary_client.post(
            f"{API}/people",
            json={"role": "Supplier", "name": "Kenya Power", "email": "billing@kp.co.ke"},
        )
        entries = admin_client.get(
            f"{API}/audit", params={"entity": "person", "action": "create"}
        ).json()["items"]
        assert entries[0]["changes"]["email"] == "billing@kp.co.ke"
