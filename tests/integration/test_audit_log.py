"""Audit visibility (SPEC §5.1, Week 1 criterion): a SELECT on vault.* produces a pgaudit and/or
error-log line that a stub parser can read.

Two kinds of line are checked:
- a denied read by agent_rw -> "permission denied for table ..." in the error log, under user agent_rw;
- an audited read of vault.* (by the superuser, whose access pgaudit still records through the
  auditor role's object grants) -> an "AUDIT: OBJECT" line.
"""

from __future__ import annotations

import csv
import io
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass

import psycopg
import pytest

from tests.support import (
    PG_CONTAINER,
    host_role,
    new_run_id,
    provision_run,
    superuser,
    teardown_run,
)

pytestmark = pytest.mark.integration

# PostgreSQL csvlog column order (docs: "Table 19.1. Logged Information" / csvlog format).
CSV_COLUMNS = {
    "log_time": 0,
    "user_name": 1,
    "database_name": 2,
    "error_severity": 11,
    "message": 13,
}


@dataclass(frozen=True)
class LogLine:
    user: str
    database: str
    severity: str
    message: str


def parse_csvlog(text: str) -> list[LogLine]:
    """Stub parser: one LogLine per csvlog record. Fails loudly on a malformed record."""
    lines: list[LogLine] = []
    for record in csv.reader(io.StringIO(text)):
        if len(record) < 14:
            raise ValueError(f"csvlog record has {len(record)} columns, expected at least 14")
        lines.append(
            LogLine(
                user=record[CSV_COLUMNS["user_name"]],
                database=record[CSV_COLUMNS["database_name"]],
                severity=record[CSV_COLUMNS["error_severity"]],
                message=record[CSV_COLUMNS["message"]],
            )
        )
    return lines


def read_server_csvlog() -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["docker", "exec", PG_CONTAINER, "sh", "-c", "cat /var/lib/postgresql/data/log/*.csv"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


@pytest.fixture(scope="module")
def audited_run() -> Iterator[str]:
    name = provision_run(new_run_id("audit"))
    try:
        yield name
    finally:
        teardown_run(name)


def test_denied_vault_read_by_agent_rw_is_logged(audited_run: str) -> None:
    with (
        host_role("agent_rw", audited_run) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("SELECT * FROM vault.users_pii_vault")

    parsed = parse_csvlog(read_server_csvlog())
    denied = [
        line
        for line in parsed
        if line.user == "agent_rw"
        and line.database == audited_run
        and line.severity == "ERROR"
        and "permission denied for" in line.message
        and "vault" in line.message
    ]
    assert denied, "no error-log line for the denied vault read"


def test_vault_select_is_recorded_by_pgaudit(audited_run: str) -> None:
    with superuser(audited_run) as conn:
        conn.execute("SELECT count(*) FROM vault.users_pii_vault").fetchone()

    parsed = parse_csvlog(read_server_csvlog())
    audit = [
        line
        for line in parsed
        if line.message.startswith("AUDIT:") and "vault.users_pii_vault" in line.message
    ]
    assert audit, "no pgaudit AUDIT line for the vault.* SELECT"
