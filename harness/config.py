"""Host-side configuration: paths, the published Postgres endpoint, and role connection strings.

Secrets are read from the environment first, then from the repo-root .env (written by `make env`).
Nothing here is ever passed into a sandbox except the test_runner URL for one run database.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

# Published on 127.0.0.1 by infra/docker-compose.yml (5432 is taken on the Windows host).
PG_HOST = "127.0.0.1"
PG_PORT = 55432
# Fixed address of the postgres container on sandbox_net (infra/docker-compose.yml).
SANDBOX_PG_HOST = "172.30.0.10"
SANDBOX_PG_PORT = 5432

GOLDEN_DB = "aegis_golden"
HARNESS_DB = "harness_db"


def _read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def secret(key: str) -> str:
    value = os.environ.get(key) or _read_env_file().get(key)
    if not value:
        raise RuntimeError(f"{key} is not set; run `make env`")
    return value


def superuser_conninfo(dbname: str) -> str:
    return (
        f"host={PG_HOST} port={PG_PORT} dbname={dbname} user=postgres "
        f"password={secret('POSTGRES_SUPERUSER_PW')}"
    )


def sandbox_dsn(dbname: str) -> str:
    """libpq URI for test_runner inside the sandbox subnet. The only credential a sandbox gets."""
    password = secret("TEST_RUNNER_PW")
    return f"postgresql://test_runner:{password}@{SANDBOX_PG_HOST}:{SANDBOX_PG_PORT}/{dbname}"


def sandbox_database_url(dbname: str) -> str:
    """Same credential in the SQLAlchemy form the victim app reads from DATABASE_URL."""
    return sandbox_dsn(dbname).replace("postgresql://", "postgresql+psycopg://", 1)


def role_conninfo(role: str, dbname: str) -> str:
    """Host-side libpq string for a role (reaches Postgres through the published port)."""
    password_key = {
        "harness": "HARNESS_PW",
        "agent_rw": "AGENT_RW_PW",
        "intake_reader": "INTAKE_READER_PW",
        "test_runner": "TEST_RUNNER_PW",
    }[role]
    return (
        f"host={PG_HOST} port={PG_PORT} dbname={dbname} user={role} password={secret(password_key)}"
    )
