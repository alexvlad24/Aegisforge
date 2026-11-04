"""Workspace construction for a run (SPEC §5.2). Uses a temp runs root; no Docker or Postgres."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.repo_lifecycle import create_workspace, destroy_workspace, git


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return create_workspace("unit_ws_0001", tmp_path)


def test_workspace_contains_visible_app_and_tests(workspace: Path) -> None:
    assert (workspace / "shop" / "services" / "vat.py").is_file()
    assert (workspace / "tests" / "test_vat.py").is_file()


def test_workspace_never_contains_hidden_tests(workspace: Path) -> None:
    assert not list(workspace.rglob("test_hidden.py"))


def test_workspace_is_a_git_repo_with_baseline_commit(workspace: Path) -> None:
    assert (workspace / ".git").is_dir()
    log = git("log", "--format=%s", cwd=workspace)
    assert log.strip() == "baseline"


def test_workspace_has_no_bytecode_or_cache(workspace: Path) -> None:
    assert not list(workspace.rglob("__pycache__"))


def test_destroy_removes_the_run_directory(tmp_path: Path) -> None:
    create_workspace("unit_ws_0002", tmp_path)
    destroy_workspace("unit_ws_0002", tmp_path)
    assert not (tmp_path / "unit_ws_0002").exists()
