"""Event schema for the append-only run log (SPEC §5.4).

Every fact about a run is one of these events, stored in harness_db.agent_events as
(run_id, seq, ts, type, payload). Run state and the model context are projections of the log
(harness/store.py); nothing else is authoritative. The agent has no tool that writes events.

Week 2 emits: RunStarted, ModelTurn, ToolCalled, ToolResult, RunResumed, RunFinished, Incident.
The remaining types are declared now so the schema is stable; their producers arrive later
(ManifestFrozen/PolicyDecision: Week 5, Compacted: Week 4, Hitl*/RunSuspended: Week 8).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Usage(_Event):
    input_tokens: int = 0  # prompt tokens, including cached ones
    cached_tokens: int = 0  # subset of input_tokens served from the provider's prompt cache
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class RunLimitsRecord(_Event):
    max_usd: float
    max_tokens: int
    max_steps: int
    max_wall_s: float


class RunStarted(_Event):
    type: Literal["RunStarted"] = "RunStarted"
    task_id: str
    task_path: str  # repo-relative path of the TaskSpec YAML
    task_sha256: str  # TaskSpec bytes at run start; resume refuses a changed TaskSpec
    run_db: str
    workspace: str  # repo-relative
    model: str
    llm_mode: Literal["record", "replay", "passthrough"]
    cassette: str  # repo-relative cassette directory
    limits: RunLimitsRecord
    trace_id: str  # 32 hex chars; resumed segments join the same trace
    root_span_id: str  # 16 hex chars
    replay_of: str | None = None  # original run id when started by `aegis replay`


class ModelTurn(_Event):
    type: Literal["ModelTurn"] = "ModelTurn"
    step: int  # 1-based model turn number within the run
    request_hash: str  # cassette key: sha256(canonical_json(request))
    message: dict[str, Any]  # assistant message exactly as it is fed back into later requests
    finish_reason: str | None
    model: str
    usage: Usage
    billed: bool  # True only if this turn hit the provider over the network
    latency_ms: float


class ToolCalled(_Event):
    type: Literal["ToolCalled"] = "ToolCalled"
    call_id: str
    name: str
    arguments: str  # raw JSON string from the model (compared verbatim by `aegis replay`)


class ToolResult(_Event):
    type: Literal["ToolResult"] = "ToolResult"
    call_id: str
    name: str
    ok: bool
    content: str
    # Reserved for information-flow labels (SPEC §5.5, Week 6). Not populated in Week 2.
    labels: dict[str, Any] | None = None


class RunResumed(_Event):
    type: Literal["RunResumed"] = "RunResumed"
    attempt: int  # 1 for the first resume
    from_seq: int  # last seq present when the resume began


RunStatus = Literal[
    "finished_unverified",  # finish_task called; hidden-test verification arrives in Week 3
    "budget_exceeded",
    "no_progress",
    "error",
]


class RunFinished(_Event):
    type: Literal["RunFinished"] = "RunFinished"
    status: RunStatus
    reason: str
    summary: str | None = None


class Incident(_Event):
    type: Literal["Incident"] = "Incident"
    kind: str
    detail: str


# ---- declared for schema stability; produced in later weeks -------------------------------


class ManifestFrozen(_Event):
    type: Literal["ManifestFrozen"] = "ManifestFrozen"
    digest: str


class PolicyDecision(_Event):
    type: Literal["PolicyDecision"] = "PolicyDecision"
    call_id: str
    decision: Literal["ALLOW", "DENY", "HITL"]
    rule_id: str
    layer: Literal["L0", "L1", "L2", "L3", "L4"]


class HitlRequested(_Event):
    type: Literal["HitlRequested"] = "HitlRequested"
    action_hash: str


class HitlResolved(_Event):
    type: Literal["HitlResolved"] = "HitlResolved"
    action_hash: str
    approved: bool


class Compacted(_Event):
    type: Literal["Compacted"] = "Compacted"
    upto_seq: int


class RunSuspended(_Event):
    type: Literal["RunSuspended"] = "RunSuspended"
    reason: str


Event = Annotated[
    RunStarted
    | ManifestFrozen
    | ModelTurn
    | ToolCalled
    | ToolResult
    | PolicyDecision
    | HitlRequested
    | HitlResolved
    | Compacted
    | Incident
    | RunSuspended
    | RunResumed
    | RunFinished,
    Field(discriminator="type"),
]

EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


class EventRecord(BaseModel):
    """One stored row: an event plus its position in the run's log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    seq: int
    ts: datetime
    event: Event


def parse_event(payload: dict[str, Any]) -> Event:
    return EVENT_ADAPTER.validate_python(payload)


def dump_event(event: Event) -> dict[str, Any]:
    dumped: dict[str, Any] = event.model_dump(mode="json")
    return dumped
