"""TaskSpec: trusted operator-written YAML (SPEC §3.3), in victim/tasks/, never in a workspace."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, StringConstraints

TaskId = Annotated[str, StringConstraints(pattern=r"^T\d{2}_[a-z0-9_]+$")]


class TaskSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: TaskId
    bug: str
    title: str
    issue: str
    writable_paths: list[str]
    visible_tests: list[str]
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    hidden_dir: str
    reference_fix: str


def load_taskspec(path: Path) -> tuple[TaskSpec, str]:
    """Return the parsed TaskSpec and the sha256 of its bytes (pinned in RunStarted)."""
    data = path.read_bytes()
    spec = TaskSpec.model_validate(yaml.safe_load(data.decode("utf-8")))
    return spec, hashlib.sha256(data).hexdigest()
