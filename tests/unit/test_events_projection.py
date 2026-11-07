"""Event schema and projection logic (SPEC §5.4). Pure; no Docker or Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from harness.events import (
    EventRecord,
    Incident,
    ModelTurn,
    RunFinished,
    RunLimitsRecord,
    RunResumed,
    RunStarted,
    ToolCalled,
    ToolResult,
    Usage,
    dump_event,
    parse_event,
)
from harness.store import NO_TOOL_NUDGE, InMemoryEventStore, SeqConflict, project

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def started() -> RunStarted:
    return RunStarted(
        task_id="T01_x",
        task_path="victim/tasks/T01_x.yaml",
        task_sha256="0" * 64,
        run_db="run_abcdef",
        workspace="runs/abcdef/workspace",
        model="gpt-4o-mini",
        llm_mode="record",
        cassette="cassettes/abcdef",
        limits=RunLimitsRecord(max_usd=1, max_tokens=10, max_steps=3, max_wall_s=60),
        trace_id="1" * 32,
        root_span_id="2" * 16,
    )


def turn(
    step: int, calls: list[tuple[str, str]], cost: float = 0.01, billed: bool = True
) -> ModelTurn:
    message: dict[str, object] = {"role": "assistant", "content": None}
    if calls:
        message["tool_calls"] = [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}
            for cid, name in calls
        ]
    return ModelTurn(
        step=step,
        request_hash="h" * 64,
        message=message,
        finish_reason="tool_calls" if calls else "stop",
        model="gpt-4o-mini",
        usage=Usage(input_tokens=100, cached_tokens=50, output_tokens=10, cost_usd=cost),
        billed=billed,
        latency_ms=1.0,
    )


def records(events: list[object], start: datetime = T0) -> list[EventRecord]:
    return [
        EventRecord(run_id="r1", seq=i + 1, ts=start + timedelta(seconds=i), event=ev)  # type: ignore[arg-type]
        for i, ev in enumerate(events)
    ]


def test_every_event_round_trips_through_json() -> None:
    events = [
        started(),
        turn(1, [("c1", "read_file")]),
        ToolCalled(call_id="c1", name="read_file", arguments='{"path":"a"}'),
        ToolResult(call_id="c1", name="read_file", ok=True, content="x"),
        RunResumed(attempt=1, from_seq=4),
        Incident(kind="harness_error", detail="boom"),
        RunFinished(status="finished_unverified", reason="finish_task", summary="s"),
    ]
    for ev in events:
        assert parse_event(dump_event(ev)) == ev


def test_unknown_type_and_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_event({"type": "AgentWroteEvent", "x": 1})
    with pytest.raises(ValidationError):
        parse_event({**dump_event(RunResumed(attempt=1, from_seq=1)), "sneaky": True})


def test_projection_builds_append_only_context_and_pending_calls() -> None:
    state = project(
        "r1",
        records(
            [
                started(),
                turn(1, [("c1", "read_file")]),
                ToolCalled(call_id="c1", name="read_file", arguments="{}"),
                ToolResult(call_id="c1", name="read_file", ok=True, content="body"),
                turn(2, [("c2", "write_file")]),
            ]
        ),
    )
    assert state.steps == 2
    assert [p.call_id for p in state.pending] == ["c2"]
    assert state.called_ids == {"c1"}
    assert [m["role"] for m in state.messages] == ["assistant", "tool", "assistant"]
    assert state.messages[1] == {"role": "tool", "tool_call_id": "c1", "content": "body"}
    assert state.input_tokens == 200 and state.cached_tokens == 100
    assert state.cost_usd == pytest.approx(0.02)


def test_turn_without_tool_call_adds_fixed_nudge() -> None:
    state = project("r1", records([started(), turn(1, []), turn(2, [])]))
    assert state.no_tool_streak == 2
    assert state.messages[1] == {"role": "user", "content": NO_TOOL_NUDGE}


def test_billed_and_replayed_cost_are_tracked_separately() -> None:
    state = project("r1", records([started(), turn(1, [], 0.5, True), turn(2, [], 0.25, False)]))
    assert state.cost_usd == pytest.approx(0.75)
    assert state.billed_usd == pytest.approx(0.5)


def test_crash_gap_does_not_count_as_active_time() -> None:
    evs = records([started(), turn(1, [])])  # active 0..1s
    resumed = EventRecord(
        run_id="r1", seq=3, ts=T0 + timedelta(hours=5), event=RunResumed(attempt=1, from_seq=2)
    )
    state = project("r1", [*evs, resumed])
    assert state.active_seconds(T0 + timedelta(hours=5, seconds=2)) == pytest.approx(3.0)


def test_successful_finish_task_result_is_projected() -> None:
    state = project(
        "r1",
        records(
            [
                started(),
                turn(1, [("c1", "finish_task")]),
                ToolCalled(call_id="c1", name="finish_task", arguments='{"summary": "done"}'),
                ToolResult(call_id="c1", name="finish_task", ok=True, content="ok"),
            ]
        ),
    )
    assert state.finish_summary == "done"
    assert state.finished is None  # the loop appends RunFinished next, also after a crash here


def test_projection_rejects_gaps_and_foreign_runs() -> None:
    recs = records([started(), turn(1, [])])
    with pytest.raises(ValueError):
        project("r1", [recs[1]])
    with pytest.raises(ValueError):
        project("other", recs)


def test_in_memory_store_enforces_contiguous_seq() -> None:
    store = InMemoryEventStore()
    store.append("r1", 1, started())
    with pytest.raises(SeqConflict):
        store.append("r1", 1, RunResumed(attempt=1, from_seq=1))
    with pytest.raises(SeqConflict):
        store.append("r1", 3, RunResumed(attempt=1, from_seq=1))
