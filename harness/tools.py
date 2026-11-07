"""Week 2 workspace tools, executed in-process by the harness.

TEMPORARY. SPEC §7 Week 2 calls tools "directly against upstream servers for now", but those
servers (servers/repo_fs.py, tests.py, ...) arrive in Week 3, which replaces this module. Until
then these functions are the whole tool surface: read_file, list_dir, grep, write_file and
finish_task. There is no run_tests: Docker belongs to servers/tests.py (Week 3).

Every path is confined to runs/<id>/workspace. write_file applies the SPEC §5.2 rules now
(defense in depth): no writes under .git/ or tests/, to conftest.py, pyproject.toml, pytest.ini or
setup.cfg, or outside the TaskSpec's writable_paths (the manifest ceiling until Week 5).

Tool outputs never contain absolute host paths, so recorded requests replay in any workspace.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_RESULT_CHARS = 20_000  # crude cap until Week 4 blob offloading (SPEC §5.4)
MAX_WRITE_BYTES = 200_000
MAX_READ_BYTES = 1_000_000
PROTECTED_DIRS = frozenset({".git", "tests"})
PROTECTED_NAMES = frozenset({"conftest.py", "pyproject.toml", "pytest.ini", "setup.cfg"})
SKIP_DIRS = frozenset({".git", "__pycache__", ".pytest_cache"})


class ToolError(Exception):
    """A tool refused or failed; the message is returned to the model as the tool result."""


@dataclass(frozen=True)
class ToolOutcome:
    ok: bool
    content: str
    finished: bool = False
    summary: str | None = None


# ------------------------------------------------------------------------------- arguments


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReadFileArgs(_Args):
    path: str = Field(min_length=1, max_length=300)
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=400, ge=1, le=2000)


class ListDirArgs(_Args):
    path: str = Field(default=".", min_length=1, max_length=300)


class GrepArgs(_Args):
    pattern: str = Field(min_length=1, max_length=200)
    path: str = Field(default=".", min_length=1, max_length=300)
    max_results: int = Field(default=100, ge=1, le=500)


class WriteFileArgs(_Args):
    path: str = Field(min_length=1, max_length=300)
    content: str


class FinishTaskArgs(_Args):
    summary: str = Field(min_length=1, max_length=2000)


def _fn(name: str, description: str, params: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": params,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


# Fixed order and fixed text: part of the static prefix (SPEC §5.4). Never filtered mid-run.
TOOL_DEFS: list[dict[str, Any]] = [
    _fn(
        "list_dir",
        "List a directory in the repository. Directories end with '/'.",
        {"path": {"type": "string", "description": "Repo-relative directory, default '.'"}},
        [],
    ),
    _fn(
        "read_file",
        "Read a text file in the repository. Lines are numbered.",
        {
            "path": {"type": "string", "description": "Repo-relative file path"},
            "start_line": {"type": "integer", "description": "First line (1-based)"},
            "max_lines": {"type": "integer", "description": "Maximum lines to return"},
        },
        ["path"],
    ),
    _fn(
        "grep",
        "Search repository files with a Python regular expression. Returns path:line: text.",
        {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Repo-relative file or directory"},
            "max_results": {"type": "integer"},
        },
        ["pattern"],
    ),
    _fn(
        "write_file",
        "Replace the full contents of a source file. Tests and repository config are read-only.",
        {
            "path": {"type": "string", "description": "Repo-relative file path"},
            "content": {"type": "string", "description": "The complete new file contents"},
        },
        ["path", "content"],
    ),
    _fn(
        "finish_task",
        "Call once the fix is complete. Ends the run.",
        {"summary": {"type": "string", "description": "One paragraph: what was changed and why"}},
        ["summary"],
    ),
]


# ----------------------------------------------------------------------------------- paths


def _rel(path: str) -> PurePosixPath:
    if "\x00" in path or "\\" in path:
        raise ToolError(f"invalid path {path!r}")
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts:
        raise ToolError(f"path must be relative to the repository root: {path!r}")
    return p


def resolve_in_workspace(workspace: Path, path: str) -> Path:
    rel = _rel(path)
    root = workspace.resolve()
    full = (root / rel).resolve()
    if full != root and not full.is_relative_to(root):
        raise ToolError(f"path escapes the repository: {path!r}")
    return full


def _display(workspace: Path, full: Path) -> str:
    rel = full.relative_to(workspace.resolve()).as_posix()
    return rel or "."


def check_writable(path: str, writable_paths: list[str]) -> PurePosixPath:
    """SPEC §5.2 write rules. Pure function so the rules are unit-tested on their own."""
    rel = _rel(path)
    parts = [p for p in rel.parts if p != "."]
    if not parts:
        raise ToolError("write_file needs a file path")
    if parts[0] in PROTECTED_DIRS or ".git" in parts:
        raise ToolError(f"write denied: {path!r} is under a read-only directory")
    if parts[-1] in PROTECTED_NAMES:
        raise ToolError(f"write denied: {parts[-1]} is read-only")
    norm = "/".join(parts)
    for allowed in writable_paths:
        if allowed.endswith("/"):
            if norm.startswith(allowed):
                return PurePosixPath(norm)
        elif fnmatch.fnmatchcase(norm, allowed):
            return PurePosixPath(norm)
    raise ToolError(f"write denied: {path!r} is outside writable paths {writable_paths}")


# ---------------------------------------------------------------------------------- tools


class Workspace:
    def __init__(self, root: Path, writable_paths: list[str]) -> None:
        self.root = root
        self.writable_paths = writable_paths

    def list_dir(self, a: ListDirArgs) -> ToolOutcome:
        full = resolve_in_workspace(self.root, a.path)
        if not full.is_dir():
            raise ToolError(f"not a directory: {a.path}")
        entries = sorted(
            f"{c.name}/" if c.is_dir() else c.name
            for c in full.iterdir()
            if c.name not in SKIP_DIRS
        )
        return ToolOutcome(True, "\n".join(entries) if entries else "(empty)")

    def read_file(self, a: ReadFileArgs) -> ToolOutcome:
        full = resolve_in_workspace(self.root, a.path)
        if not full.is_file():
            raise ToolError(f"no such file: {a.path}")
        if full.stat().st_size > MAX_READ_BYTES:
            raise ToolError(f"file too large: {a.path}")
        lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
        chunk = lines[a.start_line - 1 : a.start_line - 1 + a.max_lines]
        if not chunk:
            return ToolOutcome(True, f"(no lines at {a.start_line}; file has {len(lines)} lines)")
        body = "\n".join(f"{a.start_line + i:5d}  {line}" for i, line in enumerate(chunk))
        end = a.start_line + len(chunk) - 1
        return ToolOutcome(
            True, f"{_display(self.root, full)} lines {a.start_line}-{end} of {len(lines)}\n{body}"
        )

    def grep(self, a: GrepArgs) -> ToolOutcome:
        try:
            regex = re.compile(a.pattern)
        except re.error as exc:
            raise ToolError(f"bad regex: {exc}") from None
        base = resolve_in_workspace(self.root, a.path)
        if base.is_file():
            files = [base]
        elif base.is_dir():
            files = sorted(
                p
                for p in base.rglob("*")
                if p.is_file() and not (set(p.relative_to(self.root.resolve()).parts) & SKIP_DIRS)
            )
        else:
            raise ToolError(f"no such path: {a.path}")
        hits: list[str] = []
        for f in files:
            if f.stat().st_size > MAX_READ_BYTES:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{_display(self.root, f)}:{n}: {line}")
                    if len(hits) >= a.max_results:
                        return ToolOutcome(True, "\n".join(hits) + "\n(truncated)")
        return ToolOutcome(True, "\n".join(hits) if hits else "(no matches)")

    def write_file(self, a: WriteFileArgs) -> ToolOutcome:
        rel = check_writable(a.path, self.writable_paths)
        data = a.content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise ToolError("content too large")
        full = resolve_in_workspace(self.root, rel.as_posix())
        if full.is_dir():
            raise ToolError(f"is a directory: {a.path}")
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)  # idempotent: re-executing after a crash yields the same file
        return ToolOutcome(True, f"wrote {len(data)} bytes to {rel.as_posix()}")

    def finish_task(self, a: FinishTaskArgs) -> ToolOutcome:
        return ToolOutcome(
            True,
            "Run finished. Hidden-test verification is not enabled in this harness version.",
            finished=True,
            summary=a.summary,
        )

    def execute(self, name: str, raw_arguments: str) -> ToolOutcome:
        """Dispatch one model tool call. Never raises for model mistakes; returns ok=False."""
        handlers: dict[str, tuple[type[_Args], Callable[[Any], ToolOutcome]]] = {
            "list_dir": (ListDirArgs, self.list_dir),
            "read_file": (ReadFileArgs, self.read_file),
            "grep": (GrepArgs, self.grep),
            "write_file": (WriteFileArgs, self.write_file),
            "finish_task": (FinishTaskArgs, self.finish_task),
        }
        if name not in handlers:
            return ToolOutcome(False, f"error: unknown tool {name!r}")
        model, handler = handlers[name]
        try:
            args = model.model_validate(json.loads(raw_arguments or "{}"))
        except (json.JSONDecodeError, ValidationError) as exc:
            return ToolOutcome(False, f"error: invalid arguments for {name}: {_short(exc)}")
        try:
            outcome = handler(args)
        except ToolError as exc:
            return ToolOutcome(False, f"error: {exc}")
        except OSError as exc:
            return ToolOutcome(False, f"error: {type(exc).__name__}")
        if len(outcome.content) > MAX_RESULT_CHARS:
            cut = outcome.content[:MAX_RESULT_CHARS]
            return ToolOutcome(
                outcome.ok,
                f"{cut}\n(output truncated at {MAX_RESULT_CHARS} characters)",
                outcome.finished,
                outcome.summary,
            )
        return outcome


def _short(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
    return str(exc)
