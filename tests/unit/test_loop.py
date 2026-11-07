"""Agent loop: end-to-end run, crash-at-every-event resume, zero-network replay, tracing.

Uses the scripted provider, an in-memory event store, and a temp runs root (git only; no Docker,
no Postgres). The Postgres store and the real CLI process are covered in tests/integration.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from harness.events import EventRecord, ModelTurn, RunResumed, ToolCalled, ToolResult
from harness.loop import replay_run, resume_run, start_run
from harness.replay import ReplayMiss
from harness.store import InMemoryEventStore, project
from tests.llm_fakes import (
    T01_TASK,
    ScriptedTransport,
    make_services,
    t01_fixed_refunds,
    t01_script,
)


class SimulatedKill(BaseException):
    """BaseException, like SystemExit/KeyboardInterrupt: the loop's error handler cannot catch it,
    so the log is left exactly as a SIGKILL right after that append would leave it."""


def test_run_completes_end_to_end(tmp_path: Path) -> None:
    transport = ScriptedTransport(t01_script())
    svc = make_services(tmp_path, transport)
    out = start_run(T01_TASK, svc)

    assert out.finished.status == "finished_unverified"
    assert out.finished.summary == "Cap refunds at the remaining balance."
    ws = tmp_path / "runs" / out.run_id / "workspace"
    assert (ws / "shop/services/refunds.py").read_text("utf-8") == t01_fixed_refunds()
    types = [r.event.type for r in svc.store.load(out.run_id)]
    assert types[0] == "RunStarted" and types[-1] == "RunFinished"
    assert [n for n, _ in out.state.tool_sequence] == [
        "list_dir", "read_file", "grep", "write_file", "finish_task",
    ]  # fmt: skip
    # Write-ahead: every ToolCalled is immediately followed by its ToolResult.
    recs = svc.store.load(out.run_id)
    for a, b in zip(recs, recs[1:], strict=False):
        if isinstance(a.event, ToolCalled):
            assert isinstance(b.event, ToolResult) and b.event.call_id == a.event.call_id


def _baseline(tmp_path: Path) -> tuple[list[tuple[str, str]], int]:
    svc = make_services(tmp_path / "base", ScriptedTransport(t01_script()))
    out = start_run(T01_TASK, svc)
    return out.state.tool_sequence, len(svc.store.load(out.run_id))


def test_kill_after_any_event_then_resume_continues_from_last_event(tmp_path: Path) -> None:
    """Acceptance: killing mid-run and resuming continues from the last event. Tried at EVERY seq."""
    baseline_tools, n_events = _baseline(tmp_path)
    for kill_at in range(1, n_events):  # n_events is RunFinished: nothing left to resume
        store = InMemoryEventStore()
        transport = ScriptedTransport(t01_script())

        def die(record: EventRecord, at: int = kill_at) -> None:
            if record.seq == at:
                raise SimulatedKill

        root = tmp_path / f"k{kill_at}"
        svc = make_services(root, transport, store=store, on_event=die)
        with pytest.raises(SimulatedKill):
            start_run(T01_TASK, svc, run_id=f"kill_{kill_at:04d}")
        before = store.load(f"kill_{kill_at:04d}")
        assert len(before) == kill_at

        svc.on_event = None
        out = resume_run(f"kill_{kill_at:04d}", svc)
        log = store.load(out.run_id)

        assert log[:kill_at] == before  # history untouched (append-only)
        assert isinstance(log[kill_at].event, RunResumed)
        assert out.finished.status == "finished_unverified", kill_at
        assert out.state.tool_sequence == baseline_tools, kill_at
        steps = [r.event.step for r in log if isinstance(r.event, ModelTurn)]
        assert steps == list(range(1, len(steps) + 1)), kill_at  # no duplicated model turns
        called = [r.event.call_id for r in log if isinstance(r.event, ToolCalled)]
        assert len(called) == len(set(called)), kill_at  # no duplicated ToolCalled


def test_resume_of_finished_run_is_a_no_op(tmp_path: Path) -> None:
    svc = make_services(tmp_path, ScriptedTransport(t01_script()))
    out = start_run(T01_TASK, svc)
    n = len(svc.store.load(out.run_id))
    again = resume_run(out.run_id, svc)
    assert again.finished == out.finished
    assert len(svc.store.load(out.run_id)) == n


def test_replay_makes_zero_network_calls_and_same_tool_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: a recorded run replays with zero network calls and identical tool calls."""
    transport = ScriptedTransport(t01_script())
    svc = make_services(tmp_path, transport)
    original = start_run(T01_TASK, svc, mode="record")
    recorded_requests = len(transport.requests)

    def no_network(*_a: object, **_k: object) -> None:
        raise AssertionError("network access during replay")

    def no_live_transport() -> ScriptedTransport:
        raise AssertionError("replay built a live transport")

    import openai

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(openai.OpenAI, "__init__", no_network)
    svc.live_transport = no_live_transport

    report = replay_run(original.run_id, svc)

    assert report.identical, report.diff()
    assert report.replay.state.tool_sequence == original.state.tool_sequence
    assert len(transport.requests) == recorded_requests  # provider never consulted
    turns = [
        r.event for r in svc.store.load(report.replay.run_id) if isinstance(r.event, ModelTurn)
    ]
    assert turns and not any(t.billed for t in turns)
    assert report.replay.state.started is not None
    assert report.replay.state.started.replay_of == original.run_id


def test_replay_detects_divergence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the environment differs from the recording, the replay fails loudly (ReplayMiss)."""
    svc = make_services(tmp_path, ScriptedTransport(t01_script()))
    original = start_run(T01_TASK, svc)

    import harness.loop as loop_mod

    real_create = loop_mod.create_workspace

    def create_then_tamper(run_id: str, runs_root: Path) -> Path:
        ws = real_create(run_id, runs_root)
        (ws / "shop" / "services" / "extra.py").write_text("", encoding="utf-8")
        return ws  # list_dir output now differs -> the next request is not in the cassette

    monkeypatch.setattr(loop_mod, "create_workspace", create_then_tamper)
    with pytest.raises(ReplayMiss):
        replay_run(original.run_id, svc)


def test_one_trace_per_run_with_spans_per_turn_and_tool(tmp_path: Path) -> None:
    exporter = InMemorySpanExporter()
    store = InMemoryEventStore()

    def die(record: EventRecord) -> None:
        if record.seq == 6:
            raise SimulatedKill

    svc = make_services(tmp_path, ScriptedTransport(t01_script()), store=store, exporter=exporter,
                        on_event=die)  # fmt: skip
    with pytest.raises(SimulatedKill):
        start_run(T01_TASK, svc, run_id="trace_0001")
    svc.on_event = None
    svc.exporter = exporter
    out = resume_run("trace_0001", svc)

    spans = exporter.get_finished_spans()
    started = out.state.started
    assert started is not None
    assert {f"{s.context.trace_id:032x}" for s in spans} == {started.trace_id}  # resume included
    roots = [s for s in spans if s.parent is None]
    assert [r.name for r in roots] == ["aegis.run"]
    root_id = roots[0].context.span_id
    assert all(s.parent.span_id == root_id for s in spans if s.parent is not None)

    names = [s.name for s in spans]
    turns = [r.event for r in store.load("trace_0001") if isinstance(r.event, ModelTurn)]
    results = [r.event for r in store.load("trace_0001") if isinstance(r.event, ToolResult)]
    # Model-turn spans: one per ModelTurn (the crash at seq 6 hit after its span closed).
    assert names.count("chat gpt-4o-mini") == len(turns)
    assert sum(n.startswith("execute_tool ") for n in names) >= len(results)
    assert "aegis.run.resumed" in names and "aegis.run.finished" in names
    chat = next(s for s in spans if s.name == "chat gpt-4o-mini")
    assert chat.attributes is not None
    assert chat.attributes["gen_ai.request.model"] == "gpt-4o-mini"
    assert chat.attributes["openinference.span.kind"] == "LLM"
    assert chat.attributes["gen_ai.usage.input_tokens"] == 1200
    assert chat.attributes["gen_ai.usage.cache_read.input_tokens"] == 1024


def test_projection_of_stored_log_matches_live_state(tmp_path: Path) -> None:
    svc = make_services(tmp_path, ScriptedTransport(t01_script()))
    out = start_run(T01_TASK, svc)
    rebuilt = project(out.run_id, svc.store.load(out.run_id))
    assert rebuilt.messages == out.state.messages
    assert rebuilt.tool_sequence == out.state.tool_sequence
    assert rebuilt.cost_usd == out.state.cost_usd
