"""S-13: run databases are destroyed after teardown, and no state persists across runs (SPEC §6.1)."""

from __future__ import annotations

import asyncio

import psycopg
import pytest
from psycopg import AsyncConnection

from harness.config import superuser_conninfo
from harness.db_lifecycle import list_run_databases
from tests.support import new_run_id, provision_run, superuser, teardown_run

pytestmark = pytest.mark.security


def _run_databases() -> list[str]:
    async def go() -> list[str]:
        async with await AsyncConnection.connect(
            superuser_conninfo("postgres"), autocommit=True
        ) as admin:
            return await list_run_databases(admin)

    return asyncio.run(go())


def test_run_database_is_gone_after_teardown() -> None:
    name = provision_run(new_run_id("s13a"))
    assert name in _run_databases()
    teardown_run(name)
    assert name not in _run_databases()


def test_data_written_in_a_run_does_not_survive_into_the_next_run() -> None:
    first = provision_run(new_run_id("s13b"))
    with superuser(first) as conn:
        conn.execute(
            "INSERT INTO ops.audit_trail (action, detail) VALUES ('marker', '{\"run\": 1}')"
        )
    teardown_run(first)

    second = provision_run(new_run_id("s13c"))
    try:
        with superuser(second) as conn:
            row = conn.execute(
                "SELECT count(*) FROM ops.audit_trail WHERE action = 'marker'"
            ).fetchone()
        assert row is not None and int(row[0]) == 0
    finally:
        teardown_run(second)


def test_teardown_of_a_missing_database_fails_loudly() -> None:
    name = provision_run(new_run_id("s13d"))
    teardown_run(name)
    with pytest.raises(psycopg.Error):
        teardown_run(name)
