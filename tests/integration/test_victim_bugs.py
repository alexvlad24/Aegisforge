"""Victim app acceptance (SPEC §7 Week 1):

- visible tests run in the sandbox as test_runner against a run DB and show the 8 bugs failing;
- each hidden FAIL_TO_PASS test fails on the buggy app and passes on the reference fix;
- each PASS_TO_PASS test passes on the buggy app and on the reference fix.

Hidden tests are mounted read-only at /harness_tests and never copied into the workspace.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from harness.config import ROOT
from tests.support import (
    all_task_ids,
    apply_reference_fix,
    drop_workspace,
    find_outcome,
    load_task,
    make_workspace,
    new_run_id,
    provision_run,
    run_pytest,
    teardown_run,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def make_fixed_workspace() -> Iterator[Callable[[str], Path]]:
    created: list[str] = []

    def make(task_id: str) -> Path:
        run_id = new_run_id(f"fix_{task_id[:3].lower()}")
        created.append(run_id)
        workspace = make_workspace(run_id)
        apply_reference_fix(workspace, task_id)
        return workspace

    yield make
    for run_id in created:
        drop_workspace(run_id)


@pytest.fixture
def buggy_workspace() -> Iterator[Path]:
    run_id = new_run_id("bug_base")
    workspace = make_workspace(run_id)
    yield workspace
    drop_workspace(run_id)


def test_visible_tests_show_exactly_the_eight_bugs(buggy_workspace: Path) -> None:
    db = provision_run(new_run_id("visible"))
    try:
        result = run_pytest(["tests"], workspace=buggy_workspace, db_name=db)
    finally:
        teardown_run(db)

    outcomes = result.outcomes()
    assert outcomes, result.logs
    visible_ids = [
        test_id for task_id in all_task_ids() for test_id in load_task(task_id)["visible_tests"]
    ]
    assert len(visible_ids) == 8
    for test_id in visible_ids:
        assert find_outcome(outcomes, test_id) == "failed", test_id

    failing = [nodeid for nodeid, outcome in outcomes.items() if outcome == "failed"]
    assert len(failing) == 8, failing
    assert sum(1 for outcome in outcomes.values() if outcome == "passed") == len(outcomes) - 8


@pytest.mark.parametrize("task_id", all_task_ids())
def test_hidden_tests_fail_on_buggy_and_pass_on_reference(
    task_id: str, buggy_workspace: Path, make_fixed_workspace: Callable[[str], Path]
) -> None:
    spec = load_task(task_id)
    hidden_dir = ROOT / "victim" / spec["hidden_dir"]
    hidden_args = [f"/harness_tests/{t}" for t in [*spec["fail_to_pass"], *spec["pass_to_pass"]]]

    db = provision_run(new_run_id(f"bug_{task_id[:3].lower()}"))
    try:
        on_buggy = run_pytest(
            ["-q", *hidden_args], workspace=buggy_workspace, db_name=db, hidden_dir=hidden_dir
        )
    finally:
        teardown_run(db)
    buggy_outcomes = on_buggy.outcomes()
    for test_id in spec["fail_to_pass"]:
        assert find_outcome(buggy_outcomes, test_id) == "failed", (test_id, buggy_outcomes)
    for test_id in spec["pass_to_pass"]:
        assert find_outcome(buggy_outcomes, test_id) == "passed", (test_id, buggy_outcomes)

    fixed = make_fixed_workspace(task_id)
    db = provision_run(new_run_id(f"fix_{task_id[:3].lower()}"))
    try:
        on_fixed = run_pytest(
            ["-q", *hidden_args], workspace=fixed, db_name=db, hidden_dir=hidden_dir
        )
    finally:
        teardown_run(db)
    fixed_outcomes = on_fixed.outcomes()
    for test_id in [*spec["fail_to_pass"], *spec["pass_to_pass"]]:
        assert find_outcome(fixed_outcomes, test_id) == "passed", (test_id, fixed_outcomes)
