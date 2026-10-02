"""Aggregate router for versioned API endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
    approvals,
    audit,
    auth,
    deductions,
    people,
    reports,
    transactions,
    users,
    ws,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(people.router)
api_router.include_router(transactions.router)
api_router.include_router(deductions.router)
api_router.include_router(reports.router)
api_router.include_router(users.router)
api_router.include_router(approvals.router)
api_router.include_router(audit.router)
api_router.include_router(ws.router)
