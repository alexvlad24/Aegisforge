"""Week 2 in-process workspace tools: confinement and SPEC §5.2 write rules. No Docker/Postgres."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.repo_lifecycle import create_workspace
from harness.tools import TOOL_DEFS, ToolError, Workspace, check_writable

WRITABLE = ["shop/"]


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(create_workspace("unit_tools_01", tmp_path), WRITABLE)


def call(ws: Workspace, name: str, **args: object) -> tuple[bool, str]:
    out = ws.execute(name, json.dumps(args))
    return out.ok, out.content


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_vat.py",
        "tests/new_test.py",
        ".git/hooks/pre-commit",
        "shop/.git/config",
        "conftest.py",
        "shop/conftest.py",
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "shop/../tests/x.py",
        "/etc/passwd",
        "..\\x.py",
        "README.md",  # outside writable_paths
        "shopping/x.py",  # prefix lookalike
    ],
)
def test_write_rules_reject(path: str) -> None:
    with pytest.raises(ToolError):
        check_writable(path, WRITABLE)


def test_write_rules_accept_inside_writable_paths() -> None:
    assert check_writable("shop/services/vat.py", WRITABLE).as_posix() == "shop/services/vat.py"
    assert check_writable("./shop/x.py", WRITABLE).as_posix() == "shop/x.py"
    assert check_writable("shop/a.py", ["shop/*.py"]).as_posix() == "shop/a.py"


def test_denied_write_leaves_file_untouched(ws: Workspace) -> None:
    before = (ws.root / "tests" / "test_vat.py").read_bytes()
    ok, msg = call(ws, "write_file", path="tests/test_vat.py", content="def test(): pass\n")
    assert not ok and msg.startswith("error: write denied")
    assert (ws.root / "tests" / "test_vat.py").read_bytes() == before
    ok, _ = call(ws, "write_file", path="conftest.py", content="")
    assert not ok and not (ws.root / "conftest.py").exists()


def test_write_then_read(ws: Workspace) -> None:
    ok, msg = call(ws, "write_file", path="shop/new_mod.py", content="x = 1\n")
    assert ok and msg == "wrote 6 bytes to shop/new_mod.py"
    ok, body = call(ws, "read_file", path="shop/new_mod.py")
    assert ok and "    1  x = 1" in body


def test_reads_are_confined_to_workspace(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    (ws.root / "shop" / "link.txt").symlink_to(tmp_path / "outside.txt")
    for path in ["../outside.txt", "/etc/hostname", "shop/link.txt"]:
        ok, msg = call(ws, "read_file", path=path)
        assert not ok and "secret" not in msg, path
        assert str(tmp_path) not in msg  # no host paths leak into the context


def test_list_and_grep_skip_git_and_use_relative_paths(ws: Workspace) -> None:
    ok, listing = call(ws, "list_dir", path=".")
    assert ok and ".git" not in listing.split("\n") and "shop/" in listing
    ok, hits = call(ws, "grep", pattern="def refund_order")
    assert ok and hits.startswith("shop/services/refunds.py:")
    ok, hits = call(ws, "grep", pattern="baseline")
    assert ok and ".git" not in hits


def test_model_mistakes_become_error_results_not_exceptions(ws: Workspace) -> None:
    assert call(ws, "nope")[1].startswith("error: unknown tool")
    assert ws.execute("read_file", "{not json").content.startswith("error: invalid arguments")
    assert call(ws, "read_file", path="x", extra=1)[1].startswith("error: invalid arguments")
    assert call(ws, "grep", pattern="(")[1].startswith("error: bad regex")
    assert call(ws, "read_file", path="missing.py")[1] == "error: no such file: missing.py"


def test_finish_task_marks_outcome(ws: Workspace) -> None:
    out = ws.execute("finish_task", json.dumps({"summary": "done"}))
    assert out.ok and out.finished and out.summary == "done"


def test_tool_definitions_are_fixed() -> None:
    assert [t["function"]["name"] for t in TOOL_DEFS] == [
        "list_dir", "read_file", "grep", "write_file", "finish_task",
    ]  # fmt: skip
