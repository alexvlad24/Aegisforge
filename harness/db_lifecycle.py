"""Ephemeral per-run databases (SPEC §5.1).

provision: CREATE DATABASE run_<id> TEMPLATE aegis_golden
teardown:  DROP DATABASE run_<id> WITH (FORCE)

Both need an autocommit connection as the cluster superuser; CREATE DATABASE cannot run
inside a transaction block.
"""

from __future__ import annotations

import re

from psycopg import AsyncConnection, sql

from harness.config import GOLDEN_DB

RUN_ID_PATTERN = re.compile(r"^[a-z0-9_]{6,40}$")
RUN_DB_PREFIX = "run_"


def run_db_name(run_id: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(f"invalid run id {run_id!r}")
    return f"{RUN_DB_PREFIX}{run_id}"


async def provision(admin: AsyncConnection[tuple[object, ...]], run_id: str) -> str:
    name = run_db_name(run_id)
    await admin.execute(
        sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(name), sql.Identifier(GOLDEN_DB)
        )
    )
    return name


async def teardown(admin: AsyncConnection[tuple[object, ...]], name: str) -> None:
    if not name.startswith(RUN_DB_PREFIX):
        raise ValueError(f"refusing to drop non-run database {name!r}")
    await admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def list_run_databases(admin: AsyncConnection[tuple[object, ...]]) -> list[str]:
    cur = await admin.execute(
        "SELECT datname FROM pg_database WHERE datname LIKE 'run\\_%' ORDER BY datname"
    )
    rows = await cur.fetchall()
    return [str(row[0]) for row in rows]
