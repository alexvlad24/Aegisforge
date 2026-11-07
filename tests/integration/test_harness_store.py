"""SPEC §5.4: harness_db.agent_events is an append-only log. Needs `make up` + `make migrate`."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from harness.config import HARNESS_DB, role_conninfo
from harness.events import ModelTurn, RunResumed, Usage
from harness.store import PostgresEventStore, SeqConflict, project
from tests.support import new_run_id
from tests.unit.test_events_projection import started

pytestmark = pytest.mark.integration


@pytest.fixture
def store() -> Iterator[PostgresEventStore]:
    s = PostgresEventStore()
    s.migrate()  # idempotent
    try:
        yield s
    finally:
        s.close()


def _turn(billed: bool, cost: float) -> ModelTurn:
    return ModelTurn(
        step=1,
        request_hash="h" * 64,
        message={"role": "assistant", "content": "x"},
        finish_reason="stop",
        model="gpt-4o-mini",
        usage=Usage(input_tokens=1, cached_tokens=0, output_tokens=1, cost_usd=cost),
        billed=billed,
        latency_ms=1.0,
    )


def test_round_trip_and_projection(store: PostgresEventStore) -> None:
    run_id = new_run_id("it_store")
    a = store.append(run_id, 1, started())
    b = store.append(run_id, 2, _turn(False, 0.01))
    loaded = store.load(run_id)
    assert [r.event for r in loaded] == [a.event, b.event]
    assert [r.seq for r in loaded] == [1, 2]
    assert project(run_id, loaded).steps == 1


def test_duplicate_seq_is_a_conflict(store: PostgresEventStore) -> None:
    run_id = new_run_id("it_store")
    store.append(run_id, 1, started())
    with pytest.raises(SeqConflict):
        store.append(run_id, 1, RunResumed(attempt=1, from_seq=1))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE agent_events SET payload = '{}'::jsonb WHERE run_id = %s",
        "DELETE FROM agent_events WHERE run_id = %s",
    ],
)
def test_update_and_delete_are_rejected_by_trigger(
    store: PostgresEventStore, statement: str
) -> None:
    run_id = new_run_id("it_store")
    store.append(run_id, 1, started())
    with psycopg.connect(role_conninfo("harness", HARNESS_DB), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(statement, (run_id,))
    assert len(store.load(run_id)) == 1


def test_truncate_is_rejected_by_trigger(store: PostgresEventStore) -> None:
    with psycopg.connect(role_conninfo("harness", HARNESS_DB), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("TRUNCATE agent_events")


def test_global_spend_counts_only_billed_turns(store: PostgresEventStore) -> None:
    before = store.global_billed_usd()
    run_id = new_run_id("it_store")
    store.append(run_id, 1, started())
    store.append(run_id, 2, _turn(False, 1000.0))  # replayed: free
    assert store.global_billed_usd() == pytest.approx(before)
