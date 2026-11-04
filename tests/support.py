"""Host-side helpers shared by the Week 1 test suites.

Temporary by design: SPEC §5.3 puts the Docker socket behind the host `tests` MCP server (Week 3).
Until that server exists, the sandbox launcher lives here, and only the test suites use it.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker
import psycopg
import yaml
from psycopg import AsyncConnection

from harness.config import (
    ROOT,
    role_conninfo,
    sandbox_database_url,
    sandbox_dsn,
    superuser_conninfo,
)
from harness.db_lifecycle import provision, teardown
from harness.repo_lifecycle import create_workspace, destroy_workspace

RUNS_ROOT = ROOT / "runs"
SANDBOX_IMAGE = "aegis-sandbox:latest"
SANDBOX_NETWORK = "aegis_sandbox_net"
SECCOMP_PROFILE = ROOT / "infra" / "sandbox" / "seccomp-strict.json"
TASKS_DIR = ROOT / "victim" / "tasks"
HIDDEN_ROOT = ROOT / "victim" / "hidden_tests"
REFERENCE_DIR = ROOT / "victim" / "reference_fixes"
PG_CONTAINER = "aegis-postgres"


@dataclass
class SandboxResult:
    exit_code: int
    logs: str
    report: dict[str, Any] | None
    attrs: dict[str, Any]

    def outcomes(self) -> dict[str, str]:
        if not self.report:
            return {}
        return {t["nodeid"]: t["outcome"] for t in self.report.get("tests", [])}


def find_outcome(outcomes: dict[str, str], test_id: str) -> str:
    """Match a TaskSpec id such as `test_hidden.py::test_x` against pytest's nodeid."""
    matches = [outcome for nodeid, outcome in outcomes.items() if nodeid.endswith(test_id)]
    if len(matches) != 1:
        raise AssertionError(f"{test_id!r} matched {len(matches)} nodeids in {sorted(outcomes)}")
    return matches[0]


def load_task(task_id: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((TASKS_DIR / f"{task_id}.yaml").read_text("utf-8"))
    return loaded


def all_task_ids() -> list[str]:
    return sorted(p.stem for p in TASKS_DIR.glob("T*.yaml"))


def apply_reference_fix(workspace: Path, task_id: str) -> None:
    spec = yaml.safe_load((REFERENCE_DIR / f"{task_id}.yaml").read_text("utf-8"))
    for rep in spec["replacements"]:
        target = workspace / rep["file"]
        text = target.read_text(encoding="utf-8")
        count = text.count(rep["old"])
        if count != 1:
            raise AssertionError(f"{task_id}: expected 1 match in {rep['file']}, found {count}")
        target.write_text(text.replace(rep["old"], rep["new"]), encoding="utf-8")


# ---------------------------------------------------------------- databases


async def _provision(run_id: str) -> str:
    async with await AsyncConnection.connect(
        superuser_conninfo("postgres"), autocommit=True
    ) as admin:
        return await provision(admin, run_id)


async def _teardown(name: str) -> None:
    async with await AsyncConnection.connect(
        superuser_conninfo("postgres"), autocommit=True
    ) as admin:
        await teardown(admin, name)


def provision_run(run_id: str) -> str:
    return asyncio.run(_provision(run_id))


def teardown_run(name: str) -> None:
    asyncio.run(_teardown(name))


def new_run_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def superuser(dbname: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(superuser_conninfo(dbname), autocommit=True)


# ---------------------------------------------------------------- sandbox


REPORT_MARKER = "---AEGIS-REPORT---"


def _split_report(logs: str) -> tuple[str, dict[str, Any] | None]:
    """/tmp is tmpfs, which the archive API cannot read from a stopped container, so the report is
    printed to stdout after pytest and split back out here."""
    if REPORT_MARKER not in logs:
        return logs, None
    head, _, tail = logs.partition(REPORT_MARKER)
    payload = tail.strip()
    if not payload:
        return head, None
    loaded: dict[str, Any] = json.loads(payload)
    return head, loaded


def _seccomp_inline() -> str:
    """The daemon expects the profile as JSON in security_opt, not a host path (the SDK does not read files)."""
    return json.dumps(
        json.loads(SECCOMP_PROFILE.read_text(encoding="utf-8")), separators=(",", ":")
    )


def run_in_sandbox(
    command: list[str],
    *,
    workspace: Path | None = None,
    db_name: str | None = None,
    hidden_dir: Path | None = None,
    extra_env: dict[str, str] | None = None,
    network: str = SANDBOX_NETWORK,
    collect_report: bool = False,
) -> SandboxResult:
    """Run one command in a fresh aegis-sandbox container, then remove it.

    Mirrors SPEC §5.3: read-only root, non-root uid, no capabilities, tmpfs /tmp, pids/mem/cpu limits,
    read-only .git and tests/ inside the workspace, and only the test_runner credential for one run_* DB.
    """
    client = docker.from_env()
    volumes: dict[str, dict[str, str]] = {}
    if workspace is not None:
        volumes[str(workspace)] = {"bind": "/workspace", "mode": "rw"}
        volumes[str(workspace / "tests")] = {"bind": "/workspace/tests", "mode": "ro"}
        volumes[str(workspace / ".git")] = {"bind": "/workspace/.git", "mode": "ro"}
    if hidden_dir is not None:
        volumes[str(hidden_dir)] = {"bind": "/harness_tests", "mode": "ro"}

    env: dict[str, str] = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "/workspace"}
    if db_name is not None:
        env["DATABASE_URL"] = sandbox_database_url(db_name)
    env.update(extra_env or {})

    container = client.containers.create(
        image=SANDBOX_IMAGE,
        command=command,
        runtime="runc",
        read_only=True,
        user="10001:10001",
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true", f"seccomp={_seccomp_inline()}"],
        pids_limit=256,
        mem_limit="1g",
        nano_cpus=1_000_000_000,
        tmpfs={"/tmp": "size=256m,noexec"},
        environment=env,
        volumes=volumes,
        network=network,
        working_dir="/workspace",
    )
    try:
        container.start()
        status = container.wait(timeout=180)
        raw = container.logs().decode("utf-8", "replace")
        logs, report = _split_report(raw) if collect_report else (raw, None)
        container.reload()
        attrs = dict(container.attrs)
        return SandboxResult(
            exit_code=int(status["StatusCode"]), logs=logs, report=report, attrs=attrs
        )
    finally:
        container.remove(force=True)


PYTEST_BASE = [
    "pytest",
    "-p",
    "no:cacheprovider",
    "-q",
    "--json-report",
    "--json-report-file=/tmp/report.json",
]


def run_pytest(
    args: list[str],
    *,
    workspace: Path | None = None,
    db_name: str | None = None,
    hidden_dir: Path | None = None,
) -> SandboxResult:
    pytest_cmd = shlex.join([*PYTEST_BASE, *args])
    wrapped = [
        "sh",
        "-c",
        f"{pytest_cmd}; rc=$?; echo {REPORT_MARKER}; cat /tmp/report.json 2>/dev/null; exit $rc",
    ]
    return run_in_sandbox(
        wrapped,
        workspace=workspace,
        db_name=db_name,
        hidden_dir=hidden_dir,
        collect_report=True,
    )


# Runs inside the sandbox. Prints one JSON object with the outcome of each probe.
PROBE = r"""
import json, os, psycopg
def attempt(dsn, sql):
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            rows = conn.execute(sql).fetchall()
            return {"ok": True, "rows": len(rows)}
    except psycopg.Error as exc:
        return {"ok": False, "sqlstate": exc.sqlstate, "error": type(exc).__name__}
out = {}
out["run_db_select"] = attempt(os.environ["DATABASE_URL_LIBPQ"], "SELECT 1")
out["run_db_ops_read"] = attempt(os.environ["DATABASE_URL_LIBPQ"], "SELECT count(*) FROM ops.orders")
out["run_db_vault_read"] = attempt(os.environ["DATABASE_URL_LIBPQ"], "SELECT * FROM vault.users_pii_vault")
out["run_db_intake_read"] = attempt(os.environ["DATABASE_URL_LIBPQ"], "SELECT * FROM intake.support_tickets")
out["golden_connect"] = attempt(os.environ["GOLDEN_DSN"], "SELECT 1")
print(json.dumps(out))
"""

NET_PROBE = r"""
import json, socket
out = {}
for name, addr in {"internet_ip": ("1.1.1.1", 443), "postgres_on_subnet": ("172.30.0.10", 5432)}.items():
    try:
        socket.create_connection(addr, timeout=3).close()
        out[name] = "connected"
    except OSError as exc:
        out[name] = f"blocked:{type(exc).__name__}"
try:
    socket.getaddrinfo("example.com", 443)
    out["dns_example"] = "resolved"
except OSError as exc:
    out["dns_example"] = f"blocked:{type(exc).__name__}"
print(json.dumps(out))
"""


def probe_env(db_name: str) -> dict[str, str]:
    return {
        "DATABASE_URL_LIBPQ": sandbox_dsn(db_name),
        "GOLDEN_DSN": sandbox_dsn("aegis_golden"),
    }


def make_workspace(run_id: str) -> Path:
    return create_workspace(run_id, RUNS_ROOT)


def drop_workspace(run_id: str) -> None:
    destroy_workspace(run_id, RUNS_ROOT)


def host_role(role: str, db_name: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(role_conninfo(role, db_name), autocommit=True)
