"""S-01: agent_rw cannot read or write vault.* or intake.* (SPEC §6.1).

Enforced by grants in infra/postgres/init/02_golden.sql. Positive control: agent_rw can read ops.*.
"""

from __future__ import annotations

import psycopg
import pytest

from tests.support import host_role

pytestmark = pytest.mark.security


def test_positive_control_agent_rw_reads_ops(run_db: str) -> None:
    with host_role("agent_rw", run_db) as conn:
        count = conn.execute("SELECT count(*) FROM ops.orders").fetchone()
    assert count is not None and int(count[0]) > 0


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM vault.users_pii_vault",
        "SELECT * FROM vault.system_api_keys",
        "INSERT INTO vault.salary_ledgers (employee, amount_cents) VALUES ('x', 1)",
        "SELECT * FROM intake.support_tickets",
        "INSERT INTO intake.app_error_logs (message) VALUES ('x')",
    ],
)
def test_agent_rw_is_denied_on_zones_2_and_3(run_db: str, statement: str) -> None:
    with host_role("agent_rw", run_db) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)
