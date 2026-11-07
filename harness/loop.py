"""Model-driven agent loop (SPEC §5.4). Deterministic gates are code; the model picks tools.

Write-ahead discipline, so a run can be killed at any instant and resumed:
  1. ModelTurn is appended as soon as the response arrives (its cost is then on record).
  2. ToolCalled is appended BEFORE a tool executes; ToolResult after.
  3. On resume, a call from the last ModelTurn with no ToolResult is executed (again). Week 2
     tools are idempotent (reads, whole-file writes), so re-execution is safe. ToolCalled is not
     duplicated if it was already written.
State is never held outside the projection: every append goes through RunState.apply.

Week 2 scope: tools run in-process (harness/tools.py), no gateway, no verification gate
(finish_task ends the run as `finished_unverified`; verify.py arrives in Week 3).
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opentelemetry.sdk.trace.export import SpanExporter

from harness.budget import Budget, BudgetExceeded, RunLimits
from harness.config import ROOT
from harness.events import (
    Event,
    EventRecord,
    Incident,
    ModelTurn,
    RunFinished,
    RunResumed,
    RunStarted,
    RunStatus,
    ToolCalled,
    ToolResult,
)
from harness.llm import LLMClient, ModelsConfig
from harness.replay import Cassette, CassetteTransport, Mode, Transport
from harness.repo_lifecycle import create_workspace
from harness.store import EventStore, RunState, project
from harness.taskspec import TaskSpec, load_taskspec
from harness.telemetry import RunTracer, mark_error, set_attrs
from harness.tools import TOOL_DEFS, Workspace

RUNS_ROOT = ROOT / "runs"
CASSETTES_ROOT = ROOT / "cassettes"
MAX_NO_TOOL_TURNS = 3
FAULT_ENV = "AEGIS_FAULT_EXIT_AFTER_SEQ"  # test-only crash injection: os._exit after this seq

SYSTEM_PROMPT = """\
You are a software engineer fixing one bug in a small Python service (FastAPI + SQLAlchemy).
The repository is checked out at the repository root; all paths are relative to it.

Tools: list_dir, read_file, grep, write_file, finish_task.
- Explore only as much as needed, then fix the bug with write_file (full file contents).
- Tests under tests/ and repository config files are read-only. You cannot run tests.
- Keep the change minimal and do not change unrelated behavior.
- When the fix is complete, call finish_task with a short summary. Do not stop without it.
"""


def task_message(task: TaskSpec) -> str:
    """Trusted TaskSpec fields only (SPEC §3.3). Hidden-test details are never shown."""
    visible = "\n".join(f"- {t}" for t in task.visible_tests)
    writable = ", ".join(task.writable_paths)
    return (
        f"Task {task.id}: {task.title}\n\n"
        f"Issue:\n{task.issue.strip()}\n\n"
        f"Visible test(s) demonstrating the bug:\n{visible}\n\n"
        f"You may modify files under: {writable}"
    )


def static_prefix(task: TaskSpec) -> list[dict[str, Any]]:
    """Byte-identical for every turn of a run (and every run of the task): cacheable."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task_message(task)},
    ]


# ----------------------------------------------------------------------------- wiring


@dataclass
class Services:
    """Everything with side effects outside the event log, injectable for tests."""

    store: EventStore
    models: ModelsConfig
    live_transport: Callable[[], Transport]  # called only in record/passthrough modes
    exporter: SpanExporter | None
    provision_db: Callable[[str], str]  # run_id -> run DB name
    teardown_db: Callable[[str], None]
    runs_root: Path = RUNS_ROOT
    cassettes_root: Path = CASSETTES_ROOT
    on_event: Callable[[EventRecord], None] | None = None
    fault_exit_after_seq: int | None = None


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    finished: RunFinished
    state: RunState


def _portable(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def _from_portable(text: str) -> Path:
    p = Path(text)
    return p if p.is_absolute() else ROOT / p


def new_run_id(task: TaskSpec) -> str:
    return f"{task.id[:3].lower()}_{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------------------------ runner


class Runner:
    def __init__(
        self,
        run_id: str,
        task: TaskSpec,
        state: RunState,
        svc: Services,
        tracer: RunTracer,
    ) -> None:
        started = state.started
        if started is None:
            raise ValueError(f"run {run_id} has no RunStarted event")
        self.run_id = run_id
        self.task = task
        self.state = state
        self.svc = svc
        self.tracer = tracer
        self.started = started
        self.workspace = Workspace(_from_portable(started.workspace), task.writable_paths)
        if not self.workspace.root.is_dir():
            raise FileNotFoundError(f"workspace missing for run {run_id}: {started.workspace}")
        self.budget = Budget(
            RunLimits.from_record(started.limits),
            svc.models.total_usd_cap,
            svc.store.global_billed_usd() - state.billed_usd,
        )
        role = svc.models.roles["p_llm"]
        if role.model != started.model:
            raise ValueError(f"run pinned model {started.model}, config now says {role.model}")
        cassette = Cassette(_from_portable(started.cassette))
        mode: Mode = started.llm_mode
        live = None if mode == "replay" else svc.live_transport()
        self.llm = LLMClient(
            CassetteTransport(mode, cassette, live),
            role,
            svc.models.price_for(role.model),
            self.budget,
            cache_key=f"aegis:{task.id}",
        )
        self.prefix = static_prefix(task)

    def append(self, event: Event) -> EventRecord:
        record = self.svc.store.append(self.run_id, self.state.last_seq + 1, event)
        self.state.apply(record)
        if self.svc.on_event is not None:
            self.svc.on_event(record)
        if self.svc.fault_exit_after_seq == record.seq:
            os._exit(137)  # simulated SIGKILL: no cleanup, no flush, nothing after this seq
        return record

    def finish(self, status: RunStatus, reason: str, summary: str | None = None) -> None:
        self.append(RunFinished(status=status, reason=reason, summary=summary))

    def drive(self) -> RunFinished:
        try:
            while self.state.finished is None:
                if self.state.finish_summary is not None:
                    self.finish("finished_unverified", "finish_task", self.state.finish_summary)
                elif self.state.pending:
                    self._execute_pending()
                elif self.state.no_tool_streak >= MAX_NO_TOOL_TURNS:
                    self.finish("no_progress", f"{MAX_NO_TOOL_TURNS} turns without a tool call")
                else:
                    self._model_turn()
        except Exception as exc:
            # Not a terminal state: the run stays resumable (e.g. provider outage, replay miss).
            self.append(Incident(kind="harness_error", detail=f"{type(exc).__name__}: {exc}"[:500]))
            raise
        finished = self.state.finished
        assert finished is not None
        self._close(finished)
        return finished

    def _model_turn(self) -> None:
        step = self.state.steps + 1
        model = self.llm.role.model
        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": model,
            "llm.model_name": model,
            "aegis.run_id": self.run_id,
            "aegis.step": step,
        }
        with self.tracer.span(f"chat {model}", "LLM", attrs) as span:
            try:
                result = self.llm.chat(self.state, self.prefix + self.state.messages, TOOL_DEFS)
            except BudgetExceeded as exc:
                mark_error(span, str(exc))
                self.finish("budget_exceeded", str(exc))
                return
            u = result.usage
            calls = result.message.get("tool_calls") or []
            set_attrs(
                span,
                {
                    "gen_ai.response.model": result.model,
                    "gen_ai.response.finish_reasons": [result.finish_reason or ""],
                    "gen_ai.usage.input_tokens": u.input_tokens,
                    "gen_ai.usage.output_tokens": u.output_tokens,
                    "gen_ai.usage.cache_read.input_tokens": u.cached_tokens,
                    "llm.token_count.prompt": u.input_tokens,
                    "llm.token_count.completion": u.output_tokens,
                    "llm.token_count.prompt_details.cache_read": u.cached_tokens,
                    "aegis.cost_usd": u.cost_usd,
                    "aegis.billed": result.billed,
                    "aegis.request_hash": result.request_hash,
                    "aegis.tool_calls": ",".join(c["function"]["name"] for c in calls),
                },
            )
            self.append(
                ModelTurn(
                    step=step,
                    request_hash=result.request_hash,
                    message=result.message,
                    finish_reason=result.finish_reason,
                    model=result.model,
                    usage=u,
                    billed=result.billed,
                    latency_ms=result.latency_ms,
                )
            )
        try:
            self.budget.check_after_turn(self.state)
        except BudgetExceeded as exc:
            self.finish("budget_exceeded", str(exc))

    def _execute_pending(self) -> None:
        call = self.state.pending[0]
        if call.call_id not in self.state.called_ids:
            self.append(ToolCalled(call_id=call.call_id, name=call.name, arguments=call.arguments))
        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": call.name,
            "gen_ai.tool.call.id": call.call_id,
            "tool.name": call.name,
            "aegis.run_id": self.run_id,
        }
        with self.tracer.span(f"execute_tool {call.name}", "TOOL", attrs) as span:
            outcome = self.workspace.execute(call.name, call.arguments)
            span.set_attribute("aegis.tool.ok", outcome.ok)
            if not outcome.ok:
                mark_error(span, outcome.content[:200])
            self.append(
                ToolResult(
                    call_id=call.call_id, name=call.name, ok=outcome.ok, content=outcome.content
                )
            )

    def _close(self, finished: RunFinished) -> None:
        s = self.state
        self.tracer.marker(
            "aegis.run.finished",
            {
                "aegis.run_id": self.run_id,
                "aegis.status": finished.status,
                "aegis.steps": s.steps,
                "aegis.cost_usd": s.cost_usd,
                "aegis.tokens.input": s.input_tokens,
                "aegis.tokens.cached": s.cached_tokens,
                "aegis.tokens.output": s.output_tokens,
            },
        )
        try:
            self.svc.teardown_db(self.started.run_db)
        except Exception as exc:  # noqa: BLE001 - teardown failure must not hide the outcome
            print(f"warning: teardown of {self.started.run_db} failed: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- entry points


def start_run(
    task_path: Path,
    svc: Services,
    *,
    mode: Mode = "record",
    limits: RunLimits | None = None,
    cassette_dir: Path | None = None,
    run_id: str | None = None,
    replay_of: str | None = None,
) -> RunOutcome:
    task, task_sha = load_taskspec(task_path)
    run_id = run_id or new_run_id(task)
    workspace = create_workspace(run_id, svc.runs_root)
    run_db = svc.provision_db(run_id)
    model = svc.models.roles["p_llm"].model
    tracer = RunTracer(svc.exporter)
    trace_id, root_span_id = tracer.start_run(
        {"aegis.run_id": run_id, "aegis.task_id": task.id, "aegis.llm_mode": mode}
    )
    started = RunStarted(
        task_id=task.id,
        task_path=_portable(task_path),
        task_sha256=task_sha,
        run_db=run_db,
        workspace=_portable(workspace),
        model=model,
        llm_mode=mode,
        cassette=_portable(cassette_dir or svc.cassettes_root / run_id),
        limits=(limits or svc.models.run_defaults).to_record(),
        trace_id=trace_id,
        root_span_id=root_span_id,
        replay_of=replay_of,
    )
    state = RunState(run_id=run_id)
    record = svc.store.append(run_id, 1, started)
    state.apply(record)
    if svc.on_event is not None:
        svc.on_event(record)
    try:
        runner = Runner(run_id, task, state, svc, tracer)
        finished = runner.drive()
    finally:
        tracer.shutdown()
    return RunOutcome(run_id, finished, state)


def resume_run(run_id: str, svc: Services) -> RunOutcome:
    state = project(run_id, svc.store.load(run_id))
    started = state.started
    if started is None:
        raise LookupError(f"no events for run {run_id}")
    if state.finished is not None:
        return RunOutcome(run_id, state.finished, state)
    task, task_sha = load_taskspec(_from_portable(started.task_path))
    if task_sha != started.task_sha256:
        raise RuntimeError(f"TaskSpec {started.task_path} changed since the run started")
    tracer = RunTracer(svc.exporter)
    tracer.attach(started.trace_id, started.root_span_id)
    try:
        runner = Runner(run_id, task, state, svc, tracer)
        runner.append(RunResumed(attempt=state.resumes + 1, from_seq=state.last_seq))
        tracer.marker("aegis.run.resumed", {"aegis.run_id": run_id, "aegis.attempt": state.resumes})
        finished = runner.drive()
    finally:
        tracer.shutdown()
    return RunOutcome(run_id, finished, state)


@dataclass(frozen=True)
class ReplayReport:
    original: RunOutcome
    replay: RunOutcome

    @property
    def identical(self) -> bool:
        return (
            self.original.state.tool_sequence == self.replay.state.tool_sequence
            and self.original.finished.status == self.replay.finished.status
        )

    def diff(self) -> str:
        a, b = self.original.state.tool_sequence, self.replay.state.tool_sequence
        for i, (x, y) in enumerate(zip(a, b, strict=False)):
            if x != y:
                return f"first difference at tool call {i + 1}: {x} != {y}"
        if len(a) != len(b):
            return f"lengths differ: {len(a)} vs {len(b)}"
        return f"status {self.original.finished.status} vs {self.replay.finished.status}"


def replay_run(original_run_id: str, svc: Services) -> ReplayReport:
    """Re-run a recorded run from its cassette in a fresh workspace; never touches the network."""
    orig = project(original_run_id, svc.store.load(original_run_id))
    if orig.started is None or orig.finished is None:
        raise LookupError(f"run {original_run_id} is missing or unfinished")
    s = orig.started
    outcome = start_run(
        _from_portable(s.task_path),
        svc,
        mode="replay",
        limits=RunLimits.from_record(s.limits),
        cassette_dir=_from_portable(s.cassette),
        replay_of=original_run_id,
    )
    return ReplayReport(RunOutcome(original_run_id, orig.finished, orig), outcome)


def describe(record: EventRecord) -> str:
    ev = record.event
    match ev:
        case ModelTurn():
            calls = [c["function"]["name"] for c in ev.message.get("tool_calls") or []]
            return (
                f"ModelTurn step={ev.step} calls={calls} in={ev.usage.input_tokens} "
                f"cached={ev.usage.cached_tokens} out={ev.usage.output_tokens} "
                f"${ev.usage.cost_usd:.5f}{'' if ev.billed else ' (cassette)'}"
            )
        case ToolCalled():
            return f"ToolCalled {ev.name} {ev.arguments[:120]}"
        case ToolResult():
            return f"ToolResult {ev.name} ok={ev.ok} ({len(ev.content)} chars)"
        case RunFinished():
            return f"RunFinished {ev.status}: {ev.reason}"
        case _:
            return json.dumps({"type": ev.type})[:200]
