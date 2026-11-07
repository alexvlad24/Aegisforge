"""Create the local .env with random Postgres role passwords (never committed).

Run via `make env`. Existing values are kept, so re-running is safe.
"""

from __future__ import annotations

import secrets
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
KEYS = (
    "POSTGRES_SUPERUSER_PW",
    "HARNESS_PW",
    "AGENT_RW_PW",
    "INTAKE_READER_PW",
    "TEST_RUNNER_PW",
)


def main() -> None:
    existing: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                existing[key.strip()] = value.strip()

    lines = []
    for key in KEYS:
        value = existing.get(key) or secrets.token_urlsafe(24)
        lines.append(f"{key}={value}")
    # Keep every other entry (e.g. OPENAI_API_KEY added by the owner). An earlier version rewrote
    # the file with only KEYS and silently dropped them.
    lines.extend(f"{key}={value}" for key, value in existing.items() if key not in KEYS)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {ENV_PATH} ({len(lines)} keys)")


if __name__ == "__main__":
    main()
