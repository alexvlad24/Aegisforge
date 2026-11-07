"""Record/replay of model calls (SPEC §5.4).

A cassette is a directory of `<key>.json` files, key = sha256(canonical_json(request)). Only the
request body and the response body are stored: never headers, never the API key.

Modes
  record       hit: return the recording (no network). miss: call the provider, store, return.
  replay       hit: return the recording. miss: raise ReplayMiss. The provider is never built.
  passthrough  always call the provider; nothing is stored.

A turn is `live` (billed) only when the provider was actually called.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

Mode = Literal["record", "replay", "passthrough"]
MODES: tuple[Mode, ...] = ("record", "replay", "passthrough")


class ReplayMiss(RuntimeError):
    """Replay mode received a request that is not in the cassette."""


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_key(request: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TransportResponse:
    body: dict[str, Any]  # ChatCompletion as JSON
    live: bool  # True if the provider was called over the network


class Transport(Protocol):
    def complete(self, request: dict[str, Any]) -> TransportResponse: ...


class Cassette:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        entry: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        response: dict[str, Any] = entry["response"]
        return response

    def put(self, key: str, request: dict[str, Any], response: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"key": key, "request": request, "response": response}, indent=1),
            encoding="utf-8",
        )
        os.replace(tmp, path)  # atomic: a crash never leaves a half-written entry

    def keys(self) -> set[str]:
        if not self.directory.exists():
            return set()
        return {p.stem for p in self.directory.glob("*.json")}


class CassetteTransport:
    def __init__(self, mode: Mode, cassette: Cassette, live: Transport | None) -> None:
        if mode != "replay" and live is None:
            raise ValueError(f"mode {mode!r} needs a live transport")
        self.mode = mode
        self.cassette = cassette
        self._live = live

    def complete(self, request: dict[str, Any]) -> TransportResponse:
        key = request_key(request)
        if self.mode != "passthrough":
            recorded = self.cassette.get(key)
            if recorded is not None:
                return TransportResponse(body=recorded, live=False)
            if self.mode == "replay":
                raise ReplayMiss(
                    f"no recording for request {key} in {self.cassette.directory}; "
                    "the request differs from the recorded run"
                )
        assert self._live is not None
        response = self._live.complete(request)
        if self.mode == "record":
            self.cassette.put(key, request, response.body)
        return response
