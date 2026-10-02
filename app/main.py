"""FastAPI application factory and entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.router import api_router
from app.core.config import settings
from app.db.session import engine
from app.schemas.common import Message

logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

DESCRIPTION = """
Backend API for the Church Finance System.

* **Authentication** — email/password with short-lived JWT access tokens and
  revocable refresh tokens.
* **Ledger** — money-in / money-out entries, funds, accounts and transfers.
* **People** — members, suppliers, employees and users.
* **Accounting** — double-entry chart of accounts, trial balance and financial
  statements derived from the recorded transactions.
* **Realtime** — a WebSocket at `/api/v1/ws` pushes change events so every
  signed-in computer stays in sync.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "Starting %s v%s (%s)", settings.PROJECT_NAME, settings.VERSION, settings.ENVIRONMENT
    )
    yield
    await engine.dispose()
    logger.info("Shutdown complete")


def create_application() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        description=DESCRIPTION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
        contact={"name": "Church Finance System"},
        license_info={"name": "MIT"},
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Flatten pydantic errors into a single readable sentence."""
        problems: list[str] = []
        for error in exc.errors():
            location = " → ".join(
                str(part) for part in error["loc"] if part not in ("body", "query")
            )
            message = error["msg"].removeprefix("Value error, ")
            problems.append(f"{location}: {message}" if location else message)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": " ".join(problems) or "The submitted data is invalid."},
        )

    @app.get("/health", response_model=Message, tags=["system"], summary="Liveness probe")
    async def health() -> Message:
        return Message(message="ok")

    @app.get("/health/ready", response_model=Message, tags=["system"], summary="Readiness probe")
    async def readiness() -> Message:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return Message(message="ready")

    return app


app = create_application()
