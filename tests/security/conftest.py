"""Shared fixture: one ephemeral run database per test module (SPEC §5.1)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.support import new_run_id, provision_run, teardown_run


@pytest.fixture(scope="module")
def run_db() -> Iterator[str]:
    name = provision_run(new_run_id("sec"))
    try:
        yield name
    finally:
        teardown_run(name)
