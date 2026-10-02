-- One-time local database bootstrap for the Church Finance System.
--
-- Run from the backend/ directory:
--     psql -U postgres -h localhost -f scripts/create_database.sql
--
-- The database names below MUST match the DSNs the application actually uses:
--   * churchfinance      <- DATABASE_URL in backend/.env (see .env.example)
--   * churchfinance_test <- DATABASE_URL that tests/conftest.py injects
-- They are the single source of truth for names; if you rename a database here,
-- rename it in both of those places too, or the app and the test suite will
-- each fail to connect.

-- The optional least-privilege role. .env.example connects as `postgres`
-- (a superuser, which is why the grants below are belt-and-braces), so this role
-- is hardening rather than a requirement. To use it instead, point
-- DATABASE_URL in backend/.env at church_app with this password.
DO
$$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'church_app') THEN
        CREATE ROLE church_app LOGIN PASSWORD 'church_app_dev';
    END IF;
END
$$;

ALTER ROLE church_app WITH PASSWORD 'church_app_dev';

-- Application database.
SELECT 'CREATE DATABASE churchfinance OWNER church_app'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'churchfinance')\gexec

-- Test database, owned by the same role. pytest drops and recreates the schema
-- in here on every run, so never point this name at a database you care about.
SELECT 'CREATE DATABASE churchfinance_test OWNER church_app'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'churchfinance_test')\gexec

GRANT ALL PRIVILEGES ON DATABASE churchfinance TO church_app;
GRANT ALL PRIVILEGES ON DATABASE churchfinance_test TO church_app;
