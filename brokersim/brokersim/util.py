"""Small helpers shared by every module: simulated time, stable hashing."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any

DAY_MS = 86_400_000


def parse_iso_ms(text: str) -> int:
    """'2025-06-13T14:30:00Z' -> epoch milliseconds (UTC)."""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = _dt.datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp() * 1000)


def iso(ms: int) -> str:
    """Epoch milliseconds -> ISO-8601 UTC with millisecond precision."""
    dt = _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def trading_day(ms: int) -> str:
    """UTC calendar day, used as the 'trading day' for FIX-style dedupe scopes."""
    return iso(ms)[:10]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def stable_hash(*parts: Any, length: int = 16) -> str:
    """Deterministic hex digest of the parts (no process-random salting)."""
    h = hashlib.sha256(canonical_json(parts).encode()).hexdigest()
    return h[:length]


class SimClock:
    """Logical clock pinned to a past instant.

    Time only moves when the simulator handles a request (``tick``) or when the
    harness advances it explicitly. Wall-clock time never leaks in, so the same
    request sequence always sees the same timestamps and prices.
    """

    def __init__(self, start_ms: int, step_ms: int = 1000):
        self.start_ms = start_ms
        self.step_ms = step_ms
        self._now = start_ms
        self.calls = 0

    def now_ms(self) -> int:
        return self._now

    def tick(self) -> int:
        self.calls += 1
        self._now += self.step_ms
        return self._now

    def advance(self, ms: int) -> int:
        if ms < 0:
            raise ValueError("the simulated clock never runs backwards")
        self._now += ms
        return self._now
