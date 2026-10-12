"""Jepsen-style history.

Every request the client makes is an ``invoke`` followed by exactly one
completion as the client saw it: ``ok`` (exit 0), ``fail`` (exit 1, definitely
not applied) or ``info`` (exit 2 or a broken connection: outcome unknown). The
venue's own record of what really happened goes to a separate ledger of fills,
so a checker can compare what the agent was told with what the market did.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .util import canonical_json


class Recorder:
    def __init__(self, clock, out_dir: str | None = None):
        self.clock = clock
        self.events: list[dict] = []
        self.fills: list[dict] = []
        self.requests: list[dict] = []
        self.out_dir = out_dir
        self._files: dict[str, Any] = {}
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            for name in ("history", "ledger", "requests"):
                self._files[name] = open(os.path.join(out_dir, f"{name}.jsonl"), "w", encoding="utf-8")

    def _emit(self, stream: str, record: dict) -> None:
        f = self._files.get(stream)
        if f:
            f.write(canonical_json(record) + "\n")
            f.flush()

    def event(self, process: str, type_: str, f: str, value: dict) -> dict:
        ev = {
            "index": len(self.events),
            "time": self.clock.now_ms(),
            "process": process,
            "type": type_,
            "f": f,
            "value": value,
        }
        self.events.append(ev)
        self._emit("history", ev)
        return ev

    # Shorthands -------------------------------------------------------

    def invoke(self, f: str, value: dict) -> dict:
        return self.event("client", "invoke", f, value)

    def complete(self, f: str, exit_code: int | None, value: dict) -> dict:
        if exit_code == 0:
            t = "ok"
        elif exit_code == 1:
            t = "fail"
        else:
            t = "info"
        return self.event("client", t, f, dict(value, exit=exit_code))

    def venue(self, f: str, value: dict) -> dict:
        return self.event("venue", "event", f, value)

    def nemesis(self, f: str, value: dict) -> dict:
        return self.event("nemesis", "info", f, value)

    def fill(self, record: dict) -> None:
        rec = dict(record, seq=len(self.fills))
        self.fills.append(rec)
        self._emit("ledger", rec)
        self.event("venue", "event", "fill", rec)

    def request(self, record: dict) -> None:
        rec = dict(record, seq=len(self.requests))
        self.requests.append(rec)
        self._emit("requests", rec)

    def write_state(self, state: dict) -> None:
        if self.out_dir:
            with open(os.path.join(self.out_dir, "state.json"), "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, sort_keys=True, default=str)

    def close(self) -> None:
        for f in self._files.values():
            f.close()
        self._files.clear()


def load_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
