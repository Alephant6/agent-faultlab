"""Fault injection ("nemesis").

Faults are scripted, never random: each rule fires on the nth request that
matches it, so a scenario replays identically. Two injection points:

* ``gateway``: between the CLI and Alva's API gateway. The request may never
  arrive (``drop_before``), arrive and take effect while the reply is lost
  (``drop_after``), hang, or get an HTTP error (401, 429, 500...). Read-side
  faults also live here: ``stale`` serves market data from the past and
  ``corrupt`` garbles a file read.
* ``venue``: between Alva's broker service and the venue. The venue may time
  out after committing the order, before committing it, or commit it late;
  it may also reject, or accept and reject afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field

GATEWAY_KINDS = {"http", "drop_before", "drop_after", "hang", "delay", "stale", "corrupt"}
VENUE_KINDS = {"timeout", "unknown", "reject", "async_reject"}


@dataclass
class Rule:
    at: str
    fault: dict
    match: dict = field(default_factory=dict)
    nth: int = 1
    times: int = 1  # 0 = every match from the nth on
    id: str = ""
    seen: int = 0
    fired: int = 0

    def __post_init__(self) -> None:
        kinds = GATEWAY_KINDS if self.at == "gateway" else VENUE_KINDS
        if self.at not in ("gateway", "venue"):
            raise ValueError(f"fault rule {self.id!r}: at must be gateway or venue")
        if self.fault.get("kind") not in kinds:
            raise ValueError(f"fault rule {self.id!r}: kind must be one of {sorted(kinds)}")

    def matches(self, op: dict) -> bool:
        for k, want in self.match.items():
            got = op.get(k)
            if isinstance(want, list):
                if got not in want:
                    return False
            elif got != want:
                return False
        return True


class Nemesis:
    def __init__(self, rules: list[dict] | None, recorder):
        self.rec = recorder
        self.rules = [
            Rule(
                at=r.get("at", "gateway"),
                fault=dict(r["fault"]),
                match=dict(r.get("match", {})),
                nth=int(r.get("nth", 1)),
                times=int(r.get("times", 1)),
                id=r.get("id", f"rule{i}"),
            )
            for i, r in enumerate(rules or [])
        ]

    def pick(self, at: str, op: dict) -> dict | None:
        """Return the fault to apply to this operation, if any (first rule wins)."""
        chosen = None
        for r in self.rules:
            if r.at != at or not r.matches(op):
                continue
            r.seen += 1
            if chosen is not None:
                continue
            if r.seen < r.nth:
                continue
            if r.times and r.fired >= r.times:
                continue
            r.fired += 1
            chosen = dict(r.fault, rule=r.id)
        if chosen is not None:
            self.rec.nemesis(chosen["kind"], {"rule": chosen["rule"], "at": at, "op": op, "fault": chosen})
        return chosen

    def summary(self) -> list[dict]:
        return [{"id": r.id, "at": r.at, "kind": r.fault["kind"], "seen": r.seen, "fired": r.fired} for r in self.rules]
