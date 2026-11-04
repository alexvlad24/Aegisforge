"""SPEC §7 Week 1: provision and teardown of a run DB each complete in under 1 second."""

from __future__ import annotations

import time

import pytest

from tests.support import new_run_id, provision_run, teardown_run

pytestmark = pytest.mark.integration

ITERATIONS = 3
BUDGET_SECONDS = 1.0


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_provision_and_teardown_each_under_one_second(iteration: int) -> None:
    run_id = new_run_id("timing")

    start = time.perf_counter()
    name = provision_run(run_id)
    provision_seconds = time.perf_counter() - start

    start = time.perf_counter()
    teardown_run(name)
    teardown_seconds = time.perf_counter() - start

    print(
        f"iteration {iteration}: provision {provision_seconds:.3f}s teardown {teardown_seconds:.3f}s"
    )
    assert provision_seconds < BUDGET_SECONDS, provision_seconds
    assert teardown_seconds < BUDGET_SECONDS, teardown_seconds
