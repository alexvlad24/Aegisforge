"""`aegis` command line (SPEC §4, §7 Week 2): run | resume | replay.

Exit codes
  0  run finished (finish_task called; unverified until Week 3)
  1  harness error (run left resumable: `aegis resume <run_id>`)
  4  budget exceeded
  5  no progress (model stopped calling tools)
  6  replay diverged from the recording
  (3 is reserved for the HITL suspend of Week 8)
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Annotated

import typer
from psycopg import AsyncConnection

from harness.budget import RunLimits
from harness.config import superuser_conninfo
from harness.db_lifecycle import provision, teardown
from harness.events import EventRecord
from harness.llm import LazyOpenAITransport, load_models_config
from harness.loop import (
    FAULT_ENV,
    RunOutcome,
    Services,
    describe,
    replay_run,
    resume_run,
    start_run,
)
from harness.replay import MODES, Mode, Transport
from harness.store import PostgresEventStore
from harness.telemetry import phoenix_exporter

app = typer.Typer(add_completion=False, no_args_is_help=True, help="AegisForge harness")

EXIT_FOR_STATUS = {"finished_unverified": 0, "budget_exceeded": 4, "no_progress": 5, "error": 1}


async def _admin_provision(run_id: str) -> str:
    async with await AsyncConnection.connect(superuser_conninfo("postgres"), autocommit=True) as a:
        return await provision(a, run_id)


async def _admin_teardown(name: str) -> None:
    async with await AsyncConnection.connect(superuser_conninfo("postgres"), autocommit=True) as a:
        await teardown(a, name)


def _print_event(record: EventRecord) -> None:
    print(f"[{record.seq:4d}] {describe(record)}", file=sys.stderr, flush=True)


def _live_transport() -> Transport:
    return LazyOpenAITransport()


def _services(store: PostgresEventStore, trace: bool) -> Services:
    fault = os.environ.get(FAULT_ENV)
    return Services(
        store=store,
        models=load_models_config(),
        live_transport=_live_transport,
        exporter=phoenix_exporter() if trace else None,
        provision_db=lambda run_id: asyncio.run(_admin_provision(run_id)),
        teardown_db=lambda name: asyncio.run(_admin_teardown(name)),
        on_event=_print_event,
        fault_exit_after_seq=int(fault) if fault else None,
    )


def _report(outcome: RunOutcome) -> int:
    s = outcome.state
    f = outcome.finished
    print(
        f"run {outcome.run_id}: {f.status} ({f.reason}) steps={s.steps} "
        f"tokens in={s.input_tokens} cached={s.cached_tokens} out={s.output_tokens} "
        f"cost=${s.cost_usd:.5f} billed=${s.billed_usd:.5f}"
    )
    return EXIT_FOR_STATUS[f.status]


TraceOpt = Annotated[bool, typer.Option("--trace/--no-trace", help="Export spans to Phoenix")]


@app.command()
def run(
    task: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="TaskSpec YAML")],
    llm_mode: Annotated[str, typer.Option(help="record | replay | passthrough")] = "record",
    cassette: Annotated[Path | None, typer.Option(help="Cassette dir (default per run)")] = None,
    run_id: Annotated[str | None, typer.Option(help="Explicit run id ^[a-z0-9_]{6,40}$")] = None,
    max_usd: Annotated[float | None, typer.Option()] = None,
    max_tokens: Annotated[int | None, typer.Option()] = None,
    max_steps: Annotated[int | None, typer.Option()] = None,
    max_wall_s: Annotated[float | None, typer.Option()] = None,
    trace: TraceOpt = True,
) -> None:
    """Start a run of one TaskSpec."""
    if llm_mode not in MODES:
        raise typer.BadParameter(f"llm-mode must be one of {MODES}")
    mode: Mode = llm_mode  # validated against MODES above
    store = PostgresEventStore()
    try:
        svc = _services(store, trace)
        d = svc.models.run_defaults
        limits = RunLimits(
            max_usd=d.max_usd if max_usd is None else max_usd,
            max_tokens=d.max_tokens if max_tokens is None else max_tokens,
            max_steps=d.max_steps if max_steps is None else max_steps,
            max_wall_s=d.max_wall_s if max_wall_s is None else max_wall_s,
        )
        outcome = start_run(
            task, svc, mode=mode, limits=limits, cassette_dir=cassette, run_id=run_id
        )
    finally:
        store.close()
    raise typer.Exit(_report(outcome))


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument()],
    trace: TraceOpt = True,
) -> None:
    """Continue a run from its last event (after a crash or kill)."""
    store = PostgresEventStore()
    try:
        outcome = resume_run(run_id, _services(store, trace))
    finally:
        store.close()
    raise typer.Exit(_report(outcome))


@app.command()
def replay(
    run_id: Annotated[str, typer.Argument(help="A finished run recorded with --llm-mode record")],
    trace: TraceOpt = True,
) -> None:
    """Re-execute a recorded run from its cassette (no network) and compare tool calls."""
    store = PostgresEventStore()
    try:
        rep = replay_run(run_id, _services(store, trace))
    finally:
        store.close()
    _report(rep.replay)
    n = len(rep.replay.state.tool_sequence)
    if rep.identical:
        print(f"replay {rep.replay.run_id} of {run_id}: IDENTICAL ({n} tool calls)")
        raise typer.Exit(0)
    print(f"replay {rep.replay.run_id} of {run_id}: DIVERGED: {rep.diff()}")
    raise typer.Exit(6)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
