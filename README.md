# Church Finance System — Backend

FastAPI backend for the Church Finance System: authentication, a money-in / money-out
ledger, people records, double-entry accounting, approvals and a realtime channel.

## Stack

| Concern | Choice |
| --- | --- |
| API | FastAPI + Uvicorn |
| ORM / migrations | SQLAlchemy 2 (async) + Alembic |
| Database | PostgreSQL via `asyncpg` (Alembic uses `psycopg2`) |
| Auth | JWT access tokens (PyJWT) + revocable refresh tokens |
| Passwords | `pwdlib` with Argon2 |
| Config | `pydantic-settings`, values from the environment or `.env` |
| Tests | `pytest` + `pytest-asyncio` + `httpx` |

## Requirements

* Python 3.11 or newer (developed against 3.13)
* PostgreSQL 13 or newer

## Local setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux
pip install -e ".[dev]"
```

Copy the environment template and adjust it:

```bash
cp .env.example .env           # Windows: copy .env.example .env
```

Then create the database and apply the schema:

```bash
psql -f scripts/create_database.sql
alembic upgrade head
```

Start the development server:

```bash
uvicorn app.main:app --reload
```

The API is then available at <http://127.0.0.1:8000> with interactive docs at `/docs`.

## Configuration

Every value in `.env.example` maps to an environment variable of the same name.
Environment variables always win over the `.env` file, so nothing secret needs to
be committed.

| Variable | Purpose |
| --- | --- |
| `PROJECT_NAME`, `VERSION`, `ENVIRONMENT`, `DEBUG`, `LOG_LEVEL` | Application metadata and logging |
| `API_V1_PREFIX` | Prefix for all versioned routes (default `/api/v1`) |
| `DATABASE_URL` | Async PostgreSQL DSN. A plain `postgresql://` value is upgraded to `postgresql+asyncpg://` automatically |
| `DATABASE_SYNC_URL` | Optional blocking DSN for Alembic and scripts. Derived from `DATABASE_URL` when omitted |
| `SECRET_KEY` | Signing key for tokens, minimum 32 characters. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `ALGORITHM` | JWT signing algorithm (default `HS256`) |
| `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS`, `PASSWORD_RESET_EXPIRE_MINUTES` | Token lifetimes |
| `RETURN_RESET_LINK_IN_RESPONSE` | Returns the password reset link in the API response. Development only, keep `false` in production |
| `ALLOW_PUBLIC_REGISTRATION` | Allow anyone to sign up. Set `false` to provision accounts in PostgreSQL only |
| `FRONTEND_URL`, `CORS_ORIGINS` | Frontend origin and comma separated list of allowed origins |
| `SMTP_*` | Outbound mail for password resets. When `SMTP_HOST` is empty the reset link is logged instead |
| `DEFAULT_PAGE_SIZE`, `MAX_PAGE_SIZE` | Pagination limits |

## Endpoints

All business routes live under `/api/v1`:

| Path | Purpose |
| --- | --- |
| `POST /api/v1/auth/register`, `/login`, `/refresh`, `/logout` | Session lifecycle |
| `GET /api/v1/auth/me` | Current signed-in user |
| `POST /api/v1/auth/forgot-password`, `/reset-password`, `/change-password` | Password management |
| `GET /api/v1/auth/config` | Client configuration flags |
| `/api/v1/people` | Members, suppliers, employees and users |
| `/api/v1/transactions` | Ledger entries, funds, accounts and transfers |
| `/api/v1/deductions` | Deduction schedules |
| `/api/v1/reports` | Trial balance, financial statements and summaries |
| `/api/v1/approvals` | Approval workflow |
| `/api/v1/audit` | Audit trail |
| `WS /api/v1/ws` | Live change events; the access token is passed as `?token=` |

Unversioned system routes: `GET /health` (liveness) and `GET /health/ready`
(readiness, pings the database).

## Realtime

Browsers cannot attach an `Authorization` header to a WebSocket handshake, so the
short-lived access token is supplied as a `token` query parameter. Tokens expire after
`ACCESS_TOKEN_EXPIRE_MINUTES`; the client is expected to reconnect with a fresh one.

## Migrations

```bash
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
alembic downgrade -1
python scripts/check_migrations.py     # confirm models and migrations agree
```

`scripts/create_database.sql` holds the bootstrap SQL for a brand new database.

## Testing and quality

```bash
pytest
ruff check .
ruff format .
mypy app
```

`scripts/run_tests_isolated.py` runs the suite in a separate process when an existing
local server would otherwise hold the database.

## Deploying to Render

Create a Python web service and a PostgreSQL database in the Render dashboard, attach the
database to the service and configure the service as follows:

* **Build command** — `pip install -e .`
* **Start command** — `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"`
* **Health check path** — `/health`
* **Python version** — set `PYTHON_VERSION`

Important environment variables in production:

* `ENVIRONMENT=production` and `DEBUG=false`
* `SECRET_KEY` set to a fresh random value
* `RETURN_RESET_LINK_IN_RESPONSE=false`
* `DATABASE_URL` — Render's internal database URL can be pasted as-is; it is upgraded to
  the async driver automatically. Internal URLs only resolve from services on the same
  private network, so attach the database to the web service or use the external URL.
* `FRONTEND_URL` and `CORS_ORIGINS` — the deployed frontend origin

## License

MIT