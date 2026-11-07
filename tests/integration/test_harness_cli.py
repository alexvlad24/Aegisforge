"""SPEC §7 Week 2 acceptance, against the real stack (Postgres event store, run DBs, Phoenix).

* `aegis run` completes a run end to end.
* The `aegis` process killed mid-run (hard exit, no cleanup) and `aegis resume` continues it.
* `aegis replay` re-executes a recorded run from its cassette with identical tool calls.
* Phoenix receives one trace per run with spans per model turn and tool call.

The provider is the scripted fake (tests/llm_fakes.py): the run is recorded in-process into a
cassette, and every CLI subprocess then runs in replay mode from it, so no API key or network is
needed and nothing is billed. A real-provider run is documented in docs/reports/week_02.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from harness.config import ROOT
from harness.events import ModelTurn, RunFinished, ToolResult
from harness.loop import FAULT_ENV, RUNS_ROOT, RunOutcome, Services, resume_run, start_run
from harness.store import PostgresEventStore, project
from harness.telemetry import PHOENIX_ENDPOINT, PHOENIX_PROJECT, phoenix_exporter
from tests.llm_fakes import T01_TASK, ScriptedTransport, make_services, t01_script
from tests.support import new_run_id, provision_run, superuser, teardown_run

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def store() -> Iterator[PostgresEventStore]:
    s = PostgresEventStore()
    s.migrate()
    try:
        yield s
    finally:
        s.close()


def real_services(store: PostgresEventStore, exporter: Any = None) -> Services:
    svc = make_services(ROOT, ScriptedTransport(t01_script()), store=store, exporter=exporter)
    svc.provision_db = provision_run
    svc.teardown_db = teardown_run
    svc.runs_root = RUNS_ROOT
    svc.cassettes_root = ROOT / "cassettes"
    return svc


@pytest.fixture(scope="module")
def recorded(store: PostgresEventStore) -> RunOutcome:
    """A finished run recorded with the scripted provider (the cassette every CLI test replays)."""
    return start_run(T01_TASK, real_services(store), mode="record", run_id=new_run_id("it_rec"))


def aegis(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != FAULT_ENV}
    env.update({"PYTHONPATH": str(ROOT), **(env_extra or {})})
    return subprocess.run(
        [sys.executable, "-m", "harness.cli", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def db_exists(name: str) -> bool:
    with superuser("postgres") as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    return row is not None


def test_recorded_run_completed_end_to_end(recorded: RunOutcome, store: PostgresEventStore) -> None:
    assert recorded.finished.status == "finished_unverified"
    state = project(recorded.run_id, store.load(recorded.run_id))
    assert state.finished == recorded.finished
    assert state.started is not None and not db_exists(state.started.run_db)  # torn down


def test_cli_killed_mid_run_then_resumed(recorded: RunOutcome, store: PostgresEventStore) -> None:
    started = recorded.state.started
    assert started is not None
    run_id = new_run_id("it_kill")
    kill_at = 6  # right after a ToolCalled, before its ToolResult: the worst spot for a crash
    crashed = aegis(
        "run", str(T01_TASK), "--llm-mode", "replay", "--cassette", started.cassette,
        "--run-id", run_id, "--no-trace",
        env_extra={FAULT_ENV: str(kill_at)},
    )  # fmt: skip
    assert crashed.returncode == 137, crashed.stderr
    log = store.load(run_id)
    assert len(log) == kill_at
    assert log[-1].event.type == "ToolCalled"
    assert not any(isinstance(r.event, RunFinished) for r in log)
    run_db = f"run_{run_id}"
    assert db_exists(run_db)  # nothing was torn down: resume re-provisions nothing

    resumed = aegis("resume", run_id, "--no-trace")
    assert resumed.returncode == 0, resumed.stderr
    assert "finished_unverified" in resumed.stdout

    log = store.load(run_id)
    assert [r.seq for r in log] == list(range(1, len(log) + 1))
    assert log[kill_at].event.type == "RunResumed"
    state = project(run_id, log)
    assert state.tool_sequence == recorded.state.tool_sequence
    steps = [r.event.step for r in log if isinstance(r.event, ModelTurn)]
    assert steps == list(range(1, len(steps) + 1))
    assert not db_exists(run_db)


def test_cli_replay_is_identical(recorded: RunOutcome, store: PostgresEventStore) -> None:
    out = aegis("replay", recorded.run_id, "--no-trace")
    assert out.returncode == 0, out.stderr
    assert "IDENTICAL" in out.stdout
    replay_id = out.stdout.split("replay ", 1)[1].split()[0]
    state = project(replay_id, store.load(replay_id))
    assert state.started is not None and state.started.replay_of == recorded.run_id
    assert state.tool_sequence == recorded.state.tool_sequence
    assert state.billed_usd == 0
    assert all(not r.event.billed for r in store.load(replay_id) if isinstance(r.event, ModelTurn))


def test_cli_rejects_unknown_llm_mode() -> None:
    out = aegis("run", str(T01_TASK), "--llm-mode", "yolo", "--no-trace")
    assert out.returncode == 2


# ------------------------------------------------------------------------------- phoenix


def phoenix_spans(trace_id: str) -> list[dict[str, Any]]:
    url = f"{PHOENIX_ENDPOINT}/v1/projects/{PHOENIX_PROJECT}/spans?trace_id={trace_id}&limit=1000"
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - fixed local URL
        data: list[dict[str, Any]] = json.load(resp)["data"]
    return data


def test_phoenix_shows_one_trace_per_run_across_a_resume(store: PostgresEventStore) -> None:
    run_id = new_run_id("it_trace")
    svc = real_services(store, exporter=phoenix_exporter())

    class Kill(BaseException):
        pass

    def die(record: Any) -> None:
        if record.seq == 6:
            raise Kill

    svc.on_event = die
    with pytest.raises(Kill):
        start_run(T01_TASK, svc, run_id=run_id)
    svc.on_event = None
    out = resume_run(run_id, svc)
    started = out.state.started
    assert started is not None

    log = store.load(run_id)
    n_turns = sum(isinstance(r.event, ModelTurn) for r in log)
    n_results = sum(isinstance(r.event, ToolResult) for r in log)

    spans: list[dict[str, Any]] = []
    for _ in range(30):
        spans = phoenix_spans(started.trace_id)
        if any(s["name"] == "aegis.run.finished" for s in spans):
            break
        time.sleep(0.5)
    names = [s["name"] for s in spans]
    assert {s["context"]["trace_id"] for s in spans} == {started.trace_id}
    roots = [s for s in spans if not s.get("parent_id")]
    assert [r["name"] for r in roots] == ["aegis.run"]
    root_span_id = roots[0]["context"]["span_id"]
    assert all(s["parent_id"] == root_span_id for s in spans if s.get("parent_id"))
    assert names.count("chat gpt-4o-mini") == n_turns
    assert sum(n.startswith("execute_tool ") for n in names) >= n_results
    assert "aegis.run.resumed" in names and "aegis.run.finished" in names
    kinds = {s["name"]: s["span_kind"] for s in spans}
    assert kinds["chat gpt-4o-mini"] == "LLM"
    assert kinds["execute_tool list_dir"] == "TOOL"


def test_workspace_paths_are_inside_runs_root(recorded: RunOutcome) -> None:
    started = recorded.state.started
    assert started is not None
    assert (ROOT / started.workspace).resolve().is_relative_to(RUNS_ROOT.resolve())
    assert Path(started.cassette).parts[0] == "cassettes"
