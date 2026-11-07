"""Deterministic stand-ins for the model provider, shared by unit and integration tests.

ScriptedTransport plays a fixed script of assistant actions. The next action is chosen by the
number of assistant messages already in the request, so it is a pure function of the request:
replays and resumes of the same run see the same responses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from harness.config import ROOT
from harness.llm import load_models_config
from harness.loop import Services
from harness.replay import TransportResponse
from harness.store import EventStore, InMemoryEventStore

T01_TASK = ROOT / "victim" / "tasks" / "T01_refund_cumulative_cap.yaml"

Action = tuple[str, str, dict[str, Any]] | tuple[str, str]  # ("tool", name, args) | ("text", s)


def t01_fixed_refunds() -> str:
    """shop/services/refunds.py with the T01 reference fix applied (the scripted agent's edit)."""
    fix = yaml.safe_load(
        (ROOT / "victim" / "reference_fixes" / "T01_refund_cumulative_cap.yaml").read_text("utf-8")
    )
    text = (ROOT / "victim" / "app" / "shop" / "services" / "refunds.py").read_text("utf-8")
    for rep in fix["replacements"]:
        assert rep["file"] == "shop/services/refunds.py"
        assert text.count(rep["old"]) == 1
        text = text.replace(rep["old"], rep["new"])
    return text


def t01_script() -> list[Action]:
    return [
        ("tool", "list_dir", {"path": "shop/services"}),
        ("tool", "read_file", {"path": "shop/services/refunds.py"}),
        ("text", "Thinking out loud without a tool call."),
        ("tool", "grep", {"pattern": "def refund_order", "path": "shop"}),
        (
            "tool",
            "write_file",
            {"path": "shop/services/refunds.py", "content": t01_fixed_refunds()},
        ),
        ("tool", "finish_task", {"summary": "Cap refunds at the remaining balance."}),
    ]


class ScriptedTransport:
    def __init__(
        self,
        script: list[Action],
        *,
        usage: tuple[int, int, int] = (1200, 1024, 40),  # prompt, cached, completion
        live: bool = False,  # a fake is not the network; True only to exercise billed accounting
    ) -> None:
        self.script = script
        self.usage = usage
        self.live = live
        self.requests: list[dict[str, Any]] = []

    def complete(self, request: dict[str, Any]) -> TransportResponse:
        self.requests.append(json.loads(json.dumps(request)))
        n = sum(1 for m in request["messages"] if m["role"] == "assistant")
        action = self.script[min(n, len(self.script) - 1)]
        if action[0] == "tool":
            _, name, args = action  # type: ignore[misc]
            message: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{n:03d}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args, sort_keys=True)},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": action[1]}
            finish = "stop"
        prompt, cached, completion = self.usage
        body = {
            "id": f"chatcmpl-fake-{n}",
            "object": "chat.completion",
            "model": request.get("model", "gpt-4o-mini"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "prompt_tokens_details": {"cached_tokens": cached},
            },
        }
        return TransportResponse(body=body, live=self.live)


def make_services(
    tmp_path: Path,
    transport: ScriptedTransport,
    *,
    store: EventStore | None = None,
    exporter: Any = None,
    on_event: Any = None,
) -> Services:
    return Services(
        store=store if store is not None else InMemoryEventStore(),
        models=load_models_config(),
        live_transport=lambda: transport,
        exporter=exporter,
        provision_db=lambda run_id: f"run_{run_id}",
        teardown_db=lambda name: None,
        runs_root=tmp_path / "runs",
        cassettes_root=tmp_path / "cassettes",
        on_event=on_event,
    )
