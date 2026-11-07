"""Budget breaker, cassettes and LLM accounting (SPEC §5.4, §7 Week 2). No Docker or Postgres."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from harness.budget import RunLimits
from harness.events import ModelTurn, RunFinished
from harness.llm import Price, load_models_config, normalize_assistant_message, usage_from_body
from harness.loop import start_run
from harness.replay import Cassette, CassetteTransport, ReplayMiss, canonical_json, request_key
from harness.store import InMemoryEventStore
from tests.llm_fakes import T01_TASK, ScriptedTransport, make_services, t01_script

# ------------------------------------------------------------------------------- budget


def test_dollar_cap_stops_run_with_fake_usage(tmp_path: Path) -> None:
    """Acceptance: the breaker stops a run that exceeds a configured dollar cap."""
    # 1M prompt tokens, none cached, 0 completion at $0.15/M  =>  $0.15 per turn.
    transport = ScriptedTransport(t01_script(), usage=(1_000_000, 0, 0))
    svc = make_services(tmp_path, transport)
    limits = RunLimits(max_usd=0.40, max_tokens=10**9, max_steps=100, max_wall_s=3600)
    out = start_run(T01_TASK, svc, limits=limits)

    assert out.finished.status == "budget_exceeded"
    assert out.finished.reason.startswith("run_usd")
    # $0.15, $0.30, $0.45 -> the third turn crosses $0.40; nothing is sent after it.
    assert out.state.steps == 3
    assert len(transport.requests) == 3
    assert out.state.cost_usd == pytest.approx(0.45)
    events = [r.event for r in svc.store.load(out.run_id)]
    assert isinstance(events[-1], RunFinished)
    assert isinstance(events[-2], ModelTurn)  # the turn that tripped the breaker is recorded


def test_pre_call_check_refuses_to_send_when_global_cap_is_spent(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    # An earlier run already billed $0.30 against a cap we lower to $0.30.
    first = ScriptedTransport(t01_script(), usage=(1_000_000, 0, 0), live=True)
    svc = make_services(tmp_path, first, store=store)
    start_run(T01_TASK, svc, limits=RunLimits(0.30, 10**9, 2, 3600))
    assert store.global_billed_usd() == pytest.approx(0.30)

    second = ScriptedTransport(t01_script(), live=True)
    svc2 = make_services(tmp_path, second, store=store)
    svc2.models = dataclasses.replace(svc2.models, total_usd_cap=0.30)
    out = start_run(T01_TASK, svc2)
    assert out.finished.status == "budget_exceeded"
    assert out.finished.reason.startswith("global_usd")
    assert second.requests == []  # refused before anything went out


@pytest.mark.parametrize(
    ("limits", "prefix"),
    [
        (RunLimits(10.0, 2_500, 100, 3600), "run_tokens"),
        (RunLimits(10.0, 10**9, 2, 3600), "steps"),
        (RunLimits(10.0, 10**9, 100, 0.0), "wall_clock"),
    ],
)
def test_other_limits_trip(tmp_path: Path, limits: RunLimits, prefix: str) -> None:
    transport = ScriptedTransport(t01_script())
    out = start_run(T01_TASK, make_services(tmp_path, transport), limits=limits)
    assert out.finished.status == "budget_exceeded"
    assert out.finished.reason.startswith(prefix)


def test_replayed_turns_are_not_billed(tmp_path: Path) -> None:
    transport = ScriptedTransport(t01_script(), live=True)
    out = start_run(T01_TASK, make_services(tmp_path, transport))
    assert out.state.billed_usd == pytest.approx(out.state.cost_usd)
    assert out.state.billed_usd > 0


# ------------------------------------------------------------------------------ cassettes


def test_request_key_is_canonical() -> None:
    a = {"model": "m", "messages": [{"role": "user", "content": "é"}], "temperature": 0}
    b = {"temperature": 0, "messages": [{"content": "é", "role": "user"}], "model": "m"}
    assert request_key(a) == request_key(b)
    assert (
        canonical_json(a)
        == '{"messages":[{"content":"é","role":"user"}],"model":"m","temperature":0}'
    )
    assert request_key(a) != request_key({**a, "temperature": 1})


def test_replay_miss_fails_loudly(tmp_path: Path) -> None:
    t = CassetteTransport("replay", Cassette(tmp_path), None)
    with pytest.raises(ReplayMiss):
        t.complete({"model": "m", "messages": []})


def test_replay_mode_never_needs_a_live_transport_but_others_do(tmp_path: Path) -> None:
    CassetteTransport("replay", Cassette(tmp_path), None)
    with pytest.raises(ValueError):
        CassetteTransport("record", Cassette(tmp_path), None)


def test_record_then_replay_returns_recording(tmp_path: Path) -> None:
    live = ScriptedTransport([("text", "hi")], live=True)
    req: dict[str, Any] = {"model": "m", "messages": [{"role": "user", "content": "q"}]}
    rec = CassetteTransport("record", Cassette(tmp_path), live).complete(req)
    rep = CassetteTransport("replay", Cassette(tmp_path), None).complete(req)
    assert rec.live and not rep.live
    assert rep.body == rec.body
    assert len(live.requests) == 1


def test_passthrough_stores_nothing(tmp_path: Path) -> None:
    live = ScriptedTransport([("text", "hi")], live=True)
    CassetteTransport("passthrough", Cassette(tmp_path), live).complete({"messages": []})
    assert Cassette(tmp_path).keys() == set()


def test_cassette_holds_only_bodies_never_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_key = "sk-test-DO-NOT-LEAK-0123456789"
    monkeypatch.setenv("OPENAI_API_KEY", fake_key)
    transport = ScriptedTransport(t01_script())
    svc = make_services(tmp_path, transport)
    out = start_run(T01_TASK, svc)
    files = list((tmp_path / "cassettes" / out.run_id).glob("*.json"))
    assert files
    for f in files:
        text = f.read_text("utf-8")
        assert fake_key not in text
        assert set(json.loads(text)) == {"key", "request", "response"}
    log = json.dumps([r.model_dump(mode="json") for r in svc.store.load(out.run_id)])
    assert fake_key not in log


# -------------------------------------------------------------------------- llm accounting


def test_cost_prices_cached_tokens_at_cached_rate() -> None:
    price = Price(input=0.15, cached_input=0.075, output=0.60)
    body = {
        "usage": {
            "prompt_tokens": 2_000_000,
            "completion_tokens": 1_000_000,
            "prompt_tokens_details": {"cached_tokens": 1_000_000},
        }
    }
    u = usage_from_body(body, price)
    assert (u.input_tokens, u.cached_tokens, u.output_tokens) == (2_000_000, 1_000_000, 1_000_000)
    assert u.cost_usd == pytest.approx(0.15 + 0.075 + 0.60)


def test_models_config_is_the_source_of_model_ids() -> None:
    cfg = load_models_config()
    assert cfg.provider == "openai"
    assert cfg.roles["p_llm"].model in cfg.prices
    assert cfg.total_usd_cap == 45.0


def test_assistant_message_is_normalized_to_a_fixed_shape() -> None:
    raw = {
        "role": "assistant",
        "content": None,
        "refusal": None,
        "annotations": [],
        "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}, "x": 1}
        ],
    }
    assert normalize_assistant_message(raw) == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        ],
    }


def test_requests_extend_a_byte_identical_prefix(tmp_path: Path) -> None:
    """Cache discipline: same system/task/tools every turn; each request extends the previous."""
    transport = ScriptedTransport(t01_script())
    start_run(T01_TASK, make_services(tmp_path, transport))
    reqs = transport.requests
    assert len(reqs) >= 5
    head = canonical_json([reqs[0]["messages"][:2], reqs[0]["tools"]])
    for prev, cur in zip(reqs, reqs[1:], strict=False):
        assert canonical_json([cur["messages"][:2], cur["tools"]]) == head
        assert cur["messages"][: len(prev["messages"])] == prev["messages"]
        assert cur["prompt_cache_key"] == "aegis:T01_refund_cumulative_cap"
