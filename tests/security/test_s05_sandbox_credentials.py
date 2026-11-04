"""S-05: no DB credential other than test_runner exists in the sandbox (SPEC §6.1).

Inspects the container the way an operator would: its environment, its mounts, and its runtime
config. Each secret value from .env is checked against all of them.
"""

from __future__ import annotations

import json

import pytest

from harness.config import secret
from tests.support import PROBE, probe_env, run_in_sandbox

pytestmark = pytest.mark.security

OTHER_ROLE_KEYS = ("POSTGRES_SUPERUSER_PW", "HARNESS_PW", "AGENT_RW_PW", "INTAKE_READER_PW")


@pytest.fixture(scope="module")
def sandbox(run_db: str) -> dict[str, object]:
    # Run a real probe so the container is the same shape as a Week 1 test run.
    result = run_in_sandbox(["python", "-c", PROBE], db_name=run_db, extra_env=probe_env(run_db))
    assert result.exit_code == 0, result.logs
    return result.attrs


def _env_values(attrs: dict[str, object]) -> list[str]:
    config = attrs["Config"]
    assert isinstance(config, dict)
    env = config["Env"]
    assert isinstance(env, list)
    return [str(entry) for entry in env]


def test_no_other_role_password_in_environment(sandbox: dict[str, object]) -> None:
    env_blob = "\n".join(_env_values(sandbox))
    for key in OTHER_ROLE_KEYS:
        assert secret(key) not in env_blob, f"{key} leaked into the sandbox environment"


def test_database_url_is_test_runner_only(sandbox: dict[str, object]) -> None:
    urls = [e for e in _env_values(sandbox) if e.startswith("DATABASE_URL=")]
    assert len(urls) == 1
    assert "://test_runner:" in urls[0]


def test_only_the_test_runner_secret_is_present(sandbox: dict[str, object]) -> None:
    env_blob = "\n".join(_env_values(sandbox))
    present = [key for key in (*OTHER_ROLE_KEYS, "TEST_RUNNER_PW") if secret(key) in env_blob]
    assert present == ["TEST_RUNNER_PW"], present


def test_docker_socket_and_env_files_are_not_mounted(sandbox: dict[str, object]) -> None:
    host_config = sandbox["HostConfig"]
    assert isinstance(host_config, dict)
    binds = host_config.get("Binds") or []
    assert isinstance(binds, list)
    for bind in binds:
        text = str(bind).lower()
        assert "docker.sock" not in text
        assert "/.env" not in text.replace("\\", "/")
    mounts = sandbox["Mounts"]
    assert isinstance(mounts, list)
    for mount in mounts:
        assert "docker.sock" not in json.dumps(mount).lower()


def test_container_is_not_privileged(sandbox: dict[str, object]) -> None:
    host_config = sandbox["HostConfig"]
    assert isinstance(host_config, dict)
    assert host_config["Privileged"] is False
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["CapDrop"] == ["ALL"]
