"""S-02: test_runner cannot connect except to run_* from the sandbox subnet, and cannot read
vault.* / intake.* (SPEC §6.1). Enforced by pg_hba.conf plus grants.

Checked from both sides: the host (must be rejected by pg_hba) and a real sandbox container
(must reach its own run_* database and nothing else).
"""

from __future__ import annotations

import json

import psycopg
import pytest

from harness.config import role_conninfo
from tests.support import PROBE, probe_env, run_in_sandbox

pytestmark = pytest.mark.security


def test_host_connection_as_test_runner_is_rejected(run_db: str) -> None:
    # Host connections arrive from 172.31.0.1 (db_net), which matches no test_runner rule.
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(role_conninfo("test_runner", run_db), connect_timeout=5)


@pytest.fixture(scope="module")
def sandbox_probe(run_db: str) -> dict[str, dict[str, object]]:
    result = run_in_sandbox(
        ["python", "-c", PROBE],
        db_name=run_db,
        extra_env=probe_env(run_db),
    )
    assert result.exit_code == 0, result.logs
    loaded: dict[str, dict[str, object]] = json.loads(result.logs.strip().splitlines()[-1])
    return loaded


def test_sandbox_test_runner_reaches_its_run_database(
    sandbox_probe: dict[str, dict[str, object]],
) -> None:
    assert sandbox_probe["run_db_select"]["ok"] is True
    assert sandbox_probe["run_db_ops_read"]["ok"] is True


def test_sandbox_test_runner_cannot_connect_to_golden(
    sandbox_probe: dict[str, dict[str, object]],
) -> None:
    assert sandbox_probe["golden_connect"]["ok"] is False


def test_sandbox_test_runner_cannot_read_vault(sandbox_probe: dict[str, dict[str, object]]) -> None:
    assert sandbox_probe["run_db_vault_read"]["ok"] is False
    assert sandbox_probe["run_db_vault_read"]["sqlstate"] == "42501"


def test_sandbox_test_runner_cannot_read_intake(
    sandbox_probe: dict[str, dict[str, object]],
) -> None:
    assert sandbox_probe["run_db_intake_read"]["ok"] is False
    assert sandbox_probe["run_db_intake_read"]["sqlstate"] == "42501"
