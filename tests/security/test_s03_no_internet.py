"""S-03: the sandbox cannot reach the internet (SPEC §6.1). Enforced by the internal: true network.

Positive control: the same sandbox reaches postgres on its own subnet, so the probe is not just broken.
"""

from __future__ import annotations

import json

import pytest

from tests.support import NET_PROBE, run_in_sandbox

pytestmark = pytest.mark.security


@pytest.fixture(scope="module")
def net_probe() -> dict[str, str]:
    result = run_in_sandbox(["python", "-c", NET_PROBE])
    assert result.exit_code == 0, result.logs
    loaded: dict[str, str] = json.loads(result.logs.strip().splitlines()[-1])
    return loaded


def test_positive_control_postgres_on_sandbox_subnet_is_reachable(
    net_probe: dict[str, str],
) -> None:
    assert net_probe["postgres_on_subnet"] == "connected"


def test_public_internet_address_is_unreachable(net_probe: dict[str, str]) -> None:
    assert net_probe["internet_ip"].startswith("blocked")


def test_dns_for_external_names_fails(net_probe: dict[str, str]) -> None:
    assert net_probe["dns_example"].startswith("blocked")


@pytest.mark.parametrize("key", ["internet_ip", "dns_example"])
def test_probe_keys_are_present(net_probe: dict[str, str], key: str) -> None:
    assert key in net_probe
