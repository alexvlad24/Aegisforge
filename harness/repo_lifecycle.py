"""Per-run repository workspace (SPEC §5.2).

Every harness-side git call runs with repo hooks and config disabled:
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git -c core.hooksPath=/dev/null
    -c core.fsmonitor=false -c protocol.allow=never -c core.pager=cat --no-pager ...

Deviation from the spec's literal "git clone victim/app": victim/app is not a git repo (the
owner's repo does not commit it). The baseline snapshot is built inside runs/<id>/template
(a throwaway repo under the gitignored runs/ directory) and cloned from there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from harness.config import ROOT

VICTIM_APP = ROOT / "victim" / "app"
GIT_PREFIX = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "protocol.allow=never",
    "-c",
    "core.pager=cat",
    "--no-pager",
)
# Local-path clones need the file transport allowed explicitly once protocol.allow=never is set.
GIT_CLONE_EXTRA = ("-c", "protocol.file.allow=always")
IDENTITY = ("-c", "user.name=aegis", "-c", "user.email=aegis@localhost")


def _git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def git(*args: str, cwd: Path | None = None, clone: bool = False) -> str:
    extra = GIT_CLONE_EXTRA if clone else ()
    cmd = ["git", *GIT_PREFIX, *extra, *args]
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        cmd,
        cwd=cwd,
        env=_git_env(),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def create_workspace(run_id: str, runs_root: Path) -> Path:
    run_dir = runs_root / run_id
    template = run_dir / "template"
    workspace = run_dir / "workspace"
    if run_dir.exists():
        raise FileExistsError(f"run directory already exists: {run_dir}")

    shutil.copytree(
        VICTIM_APP,
        template,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
    )
    git("init", "-q", cwd=template)
    git("add", "-A", cwd=template)
    git(*IDENTITY, "commit", "-q", "-m", "baseline", cwd=template)
    git("clone", "-q", "--no-hardlinks", str(template), str(workspace), cwd=run_dir, clone=True)
    return workspace


def destroy_workspace(run_id: str, runs_root: Path) -> None:
    run_dir = runs_root / run_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
