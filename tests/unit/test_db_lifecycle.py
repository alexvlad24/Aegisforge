"""Pure-logic checks for the run-database lifecycle (no Postgres needed)."""

from __future__ import annotations

import asyncio

import pytest

from harness.db_lifecycle import run_db_name, teardown


@pytest.mark.parametrize("run_id", ["abc123", "run_0001", "a" * 40])
def test_valid_run_ids_map_to_run_prefixed_names(run_id: str) -> None:
    assert run_db_name(run_id) == f"run_{run_id}"


@pytest.mark.parametrize(
    "run_id",
    ["short", "UPPERCASE_ID", "has-dash", "x; DROP DATABASE aegis_golden", "a" * 41, ""],
)
def test_invalid_run_ids_are_rejected(run_id: str) -> None:
    with pytest.raises(ValueError):
        run_db_name(run_id)


def test_teardown_refuses_non_run_databases() -> None:
    # The guard runs before any connection is used, so no admin connection is needed here.
    with pytest.raises(ValueError):
        asyncio.run(teardown(None, "aegis_golden"))  # type: ignore[arg-type]
