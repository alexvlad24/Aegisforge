"""Event store and projections (SPEC §5.4).

The log in harness_db.agent_events is the only source of truth for a run. `RunState` is a pure
left fold over the log (`project`), so the context sent to the model, the step count, the spend,
and the calls still owed a result are all derived, never stored separately. `aegis resume`
rebuilds everything from here.

Append-only is enforced twice: the store API has no update/delete, and a trigger in harness_db
rejects UPDATE, DELETE and TRUNCATE on the table.

Usage:  python -m harness.store migrate     (idempotent; run by `make migrate`)
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from harness.config import HARNESS_DB, role_conninfo
from harness.events import (
    Event,
    EventRecord,
    ModelTurn,
    RunFinished,
    RunResumed,
    RunStarted,
    ToolCalled,
    ToolResult,
    dump_event,
    parse_event,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_events (
    run_id  text        NOT NULL,
    seq     integer     NOT NULL CHECK (seq >= 1),
    ts      timestamptz NOT NULL,
    type    text        NOT NULL,
    payload jsonb       NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS agent_events_type_idx ON agent_events (type);

CREATE OR REPLACE FUNCTION agent_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'agent_events is append-only (% rejected)', TG_OP;
END
$$;

DROP TRIGGER IF EXISTS agent_events_no_update_delete ON agent_events;
CREATE TRIGGER agent_events_no_update_delete
    BEFORE UPDATE OR DELETE ON agent_events
    FOR EACH ROW EXECUTE FUNCTION agent_events_append_only();

DROP TRIGGER IF EXISTS agent_events_no_truncate ON agent_events;
CREATE TRIGGER agent_events_no_truncate
    BEFORE TRUNCATE ON agent_events
    FOR EACH STATEMENT EXECUTE FUNCTION agent_events_append_only();
"""

# Fed back to the model after a turn with no tool call. Fixed text, so the projection stays
# deterministic and replays produce identical requests.
NO_TOOL_NUDGE = (
    "No tool was called. Continue the task using the tools, "
    "or call finish_task when the fix is complete."
)


class SeqConflict(RuntimeError):
    """Another writer appended at the same (run_id, seq): two processes are driving one run."""


# ------------------------------------------------------------------------------- projection


@dataclass(frozen=True)
class PendingCall:
    call_id: str
    name: str
    arguments: str


@dataclass
class RunState:
    run_id: str
    started: RunStarted | None = None
    last_seq: int = 0
    steps: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0  # every turn, billed or replayed (per-run limits behave identically)
    billed_usd: float = 0.0  # turns that actually hit the provider
    messages: list[dict[str, Any]] = field(default_factory=list)  # append-only context suffix
    pending: list[PendingCall] = field(default_factory=list)  # tool calls owed a ToolResult
    called_ids: set[str] = field(default_factory=set)  # call_ids with a ToolCalled event
    tool_sequence: list[tuple[str, str]] = field(default_factory=list)  # (name, raw args)
    call_args: dict[str, str] = field(default_factory=dict)  # call_id -> raw args
    finish_summary: str | None = None  # set by a successful finish_task ToolResult
    no_tool_streak: int = 0
    resumes: int = 0
    finished: RunFinished | None = None
    closed_active_s: float = 0.0  # active time of earlier process segments
    segment_start: datetime | None = None
    last_ts: datetime | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def apply(self, record: EventRecord) -> None:
        if record.run_id != self.run_id:
            raise ValueError(f"event for {record.run_id} applied to {self.run_id}")
        if record.seq != self.last_seq + 1:
            raise ValueError(f"{self.run_id}: expected seq {self.last_seq + 1}, got {record.seq}")
        ev = record.event
        match ev:
            case RunStarted():
                if self.started is not None:
                    raise ValueError("duplicate RunStarted")
                self.started = ev
                self.segment_start = record.ts
            case ModelTurn():
                self.steps = ev.step
                self.input_tokens += ev.usage.input_tokens
                self.cached_tokens += ev.usage.cached_tokens
                self.output_tokens += ev.usage.output_tokens
                self.cost_usd += ev.usage.cost_usd
                if ev.billed:
                    self.billed_usd += ev.usage.cost_usd
                self.messages.append(ev.message)
                calls = ev.message.get("tool_calls") or []
                self.pending = [
                    PendingCall(c["id"], c["function"]["name"], c["function"]["arguments"])
                    for c in calls
                ]
                if calls:
                    self.no_tool_streak = 0
                else:
                    self.no_tool_streak += 1
                    self.messages.append({"role": "user", "content": NO_TOOL_NUDGE})
            case ToolCalled():
                self.called_ids.add(ev.call_id)
                self.call_args[ev.call_id] = ev.arguments
                self.tool_sequence.append((ev.name, ev.arguments))
            case ToolResult():
                self.pending = [p for p in self.pending if p.call_id != ev.call_id]
                self.messages.append(
                    {"role": "tool", "tool_call_id": ev.call_id, "content": ev.content}
                )
                if ev.name == "finish_task" and ev.ok:
                    self.finish_summary = _summary(self.call_args.get(ev.call_id, "{}"))
            case RunResumed():
                if self.segment_start is not None and self.last_ts is not None:
                    self.closed_active_s += (self.last_ts - self.segment_start).total_seconds()
                self.segment_start = record.ts
                self.resumes = ev.attempt
            case RunFinished():
                self.finished = ev
            case _:
                pass  # later-week events carry no Week 2 state
        self.last_seq = record.seq
        self.last_ts = record.ts

    def active_seconds(self, now: datetime) -> float:
        """Wall-clock spent inside harness processes; the gap between a crash and resume is free."""
        if self.segment_start is None:
            return self.closed_active_s
        return self.closed_active_s + (now - self.segment_start).total_seconds()


def _summary(raw_args: str) -> str:
    try:
        parsed = json.loads(raw_args)
    except json.JSONDecodeError:
        return ""
    return str(parsed.get("summary", "")) if isinstance(parsed, dict) else ""


def project(run_id: str, records: Iterable[EventRecord]) -> RunState:
    state = RunState(run_id=run_id)
    for record in records:
        state.apply(record)
    return state


# ------------------------------------------------------------------------------------ stores


class EventStore(Protocol):
    def append(self, run_id: str, seq: int, event: Event) -> EventRecord: ...

    def load(self, run_id: str) -> list[EventRecord]: ...

    def global_billed_usd(self) -> float: ...


def _now() -> datetime:
    return datetime.now(UTC)


class InMemoryEventStore:
    """Same contract as the Postgres store; used by unit tests."""

    def __init__(self) -> None:
        self._rows: dict[str, list[EventRecord]] = {}

    def append(self, run_id: str, seq: int, event: Event) -> EventRecord:
        rows = self._rows.setdefault(run_id, [])
        if seq != len(rows) + 1:
            raise SeqConflict(f"{run_id}: seq {seq} but log has {len(rows)} events")
        record = EventRecord(run_id=run_id, seq=seq, ts=_now(), event=event)
        rows.append(record)
        return record

    def load(self, run_id: str) -> list[EventRecord]:
        return list(self._rows.get(run_id, []))

    def global_billed_usd(self) -> float:
        return sum(
            r.event.usage.cost_usd
            for rows in self._rows.values()
            for r in rows
            if isinstance(r.event, ModelTurn) and r.event.billed
        )


class PostgresEventStore:
    """harness_db.agent_events, connected as role `harness` (never reachable from a sandbox)."""

    def __init__(self, conninfo: str | None = None) -> None:
        self._conn = psycopg.connect(conninfo or role_conninfo("harness", HARNESS_DB))
        self._conn.autocommit = True

    def close(self) -> None:
        self._conn.close()

    def migrate(self) -> None:
        with self._conn.transaction():
            self._conn.execute(SCHEMA_SQL)

    def append(self, run_id: str, seq: int, event: Event) -> EventRecord:
        ts = _now()
        try:
            self._conn.execute(
                "INSERT INTO agent_events (run_id, seq, ts, type, payload) "
                "VALUES (%s, %s, %s, %s, %s)",
                (run_id, seq, ts, event.type, Jsonb(dump_event(event))),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise SeqConflict(f"{run_id}: seq {seq} already written") from exc
        return EventRecord(run_id=run_id, seq=seq, ts=ts, event=event)

    def load(self, run_id: str) -> list[EventRecord]:
        rows = self._conn.execute(
            "SELECT seq, ts, payload FROM agent_events WHERE run_id = %s ORDER BY seq",
            (run_id,),
        ).fetchall()
        return [
            EventRecord(run_id=run_id, seq=int(seq), ts=ts, event=parse_event(payload))
            for seq, ts, payload in rows
        ]

    def global_billed_usd(self) -> float:
        row = self._conn.execute(
            "SELECT coalesce(sum((payload->'usage'->>'cost_usd')::numeric), 0) "
            "FROM agent_events WHERE type = 'ModelTurn' AND (payload->>'billed')::boolean"
        ).fetchone()
        return float(row[0]) if row else 0.0


def main(argv: list[str]) -> int:
    if argv[1:] != ["migrate"]:
        print("usage: python -m harness.store migrate", file=sys.stderr)
        return 2
    store = PostgresEventStore()
    try:
        store.migrate()
    finally:
        store.close()
    print("harness_db.agent_events: migrated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
