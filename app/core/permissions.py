"""Which role may do what.

The church office splits into three jobs, and each person should only be able to
do their own. Rather than testing flags at every route, each endpoint asks for one
:class:`~app.enums.Permission` and the rules are answered from the table below.

The tiers, in one sentence each:

* **Accountant** - the money. Records in and out, moves money between accounts,
  runs the accounting reports, and decides approval requests.
* **Secretary** - the people and the money coming in. Keeps the directory,
  records collections, reads the reports, and can ask for money out.
* **Administrator** - all of the above, plus provisioning accounts and reading the
  audit trail.

Everyone can see the dashboard, which is why it has its own permission but grants
nothing that a role needs to hold for any other reason.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException, status

from app.core.deps import CurrentUser
from app.db.models import User
from app.enums import Permission, UserRole

_ACCOUNTANT: frozenset[Permission] = frozenset(
    {
        Permission.DASHBOARD_VIEW,
        Permission.PEOPLE_VIEW,
        Permission.MONEY_IN,
        Permission.MONEY_OUT,
        Permission.TRANSFERS_MANAGE,
        Permission.DEDUCTIONS_MANAGE,
        Permission.ACCOUNTING_VIEW,
        Permission.REPORTS_VIEW,
        Permission.APPROVALS_VIEW,
        Permission.APPROVALS_REQUEST,
        Permission.APPROVALS_DECIDE,
    }
)

_SECRETARY: frozenset[Permission] = frozenset(
    {
        Permission.DASHBOARD_VIEW,
        Permission.PEOPLE_VIEW,
        Permission.PEOPLE_MANAGE,
        Permission.MONEY_IN,
        Permission.REPORTS_VIEW,
        Permission.APPROVALS_VIEW,
        Permission.APPROVALS_REQUEST,
    }
)

ROLE_PERMISSIONS: dict[UserRole, frozenset[Permission]] = {
    UserRole.ACCOUNTANT: _ACCOUNTANT,
    UserRole.SECRETARY: _SECRETARY,
    # An administrator has to be able to do the accountant's job, or the church
    # is stuck whenever the treasurer is away.
    UserRole.ADMIN: _ACCOUNTANT | _SECRETARY | {Permission.USERS_MANAGE, Permission.AUDIT_VIEW},
}

#: Shown to the client so the interface can hide what the server would refuse.
#: Exposed by GET /auth/me and the user list.
ALL_PERMISSIONS: frozenset[Permission] = frozenset(
    permission for granted in ROLE_PERMISSIONS.values() for permission in granted
)


def permissions_for(role: UserRole) -> frozenset[Permission]:
    """What ``role`` may do."""
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(role: UserRole, permission: Permission) -> bool:
    """Whether ``role`` grants ``permission``."""
    return permission in permissions_for(role)


def ensure_permission(user: User, permission: Permission) -> None:
    """Raise 403 unless ``user`` holds ``permission``.

    The dependency form below covers a whole endpoint, but a few endpoints serve
    two kinds of work - ``POST /transactions`` is money-in or money-out depending
    on the body - and those have to decide once the payload is known. Refusing here
    rather than at the start keeps one wording for every rejection.
    """
    if not has_permission(user.role, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"An {user.role.label} account cannot {permission.activity}.",
        )


def require_permission(permission: Permission) -> Callable[[User], User]:
    """Build a dependency that admits only roles holding ``permission``.

    Used as ``user: Annotated[User, Depends(require_permission(Permission.X))]``.
    The dependency hands back the same user object, so a route that needs both the
    authorisation and the actor for the audit trail only declares it once.
    """

    def dependency(user: CurrentUser) -> User:
        ensure_permission(user, permission)
        return user

    return dependency


__all__ = [
    "ALL_PERMISSIONS",
    "ROLE_PERMISSIONS",
    "ensure_permission",
    "has_permission",
    "permissions_for",
    "require_permission",
]