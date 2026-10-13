"""Agent Schedules over GraphQL (POST /query): the operations `alva schedule`
sends (toolkit 0.31.0, src/resources/schedules.ts).

A scenario lists schedules in the CLI's own output form:

    {"name": "btc-daily-buy", "rule": {"kind": "every", "interval": "P1D"},
     "bounds": {"startsAt": "2025-06-09T14:30:00Z", "maxOccurrences": 14},
     "text": "Buy 0.01 BTC ...", "createdAt": "2025-06-08T21:04:12Z"}

Rule kinds are "at" (timestamp), "every" (interval, an ISO-8601 duration)
and "cron" (expression, timezone). occurrencesUsed and nextFireAt follow
from the rule, the bounds and the clock for "at" and "every"; a "cron"
schedule, or any schedule that should disagree with its rule, gives them
explicitly. "status": "paused" pauses one.
"""

from __future__ import annotations

import re

from .util import parse_iso_ms

OPERATION = re.compile(r"^\s*(query|mutation)\s+(\w+)")
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


class GraphQLError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def duration_s(text: str) -> int:
    m = DURATION.match(text or "")
    if not m or not any(m.groups()):
        raise ValueError(f"not an ISO-8601 duration: {text!r}")
    d, h, mi, s = (int(g or 0) for g in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def _ms(value) -> int | None:
    return None if value is None else parse_iso_ms(value)


class Schedules:
    def __init__(self, clock, defs: list[dict] | None, channel_id: str):
        self.clock = clock
        self.channel_id = str(channel_id)
        self.items: dict[str, dict] = {}
        for i, d in enumerate(defs or []):
            s = self._from_scenario(d, i)
            self.items[s["name"]] = s

    # ------------------------------------------------------------ state

    def _from_scenario(self, d: dict, i: int) -> dict:
        r = d["rule"]
        kind = r["kind"].lower()
        if kind == "at":
            rule = {"kind": "AT", "atMs": parse_iso_ms(r["timestamp"])}
        elif kind == "every":
            rule = {"kind": "EVERY", "everyIntervalSeconds": duration_s(r["interval"])}
        elif kind == "cron":
            rule = {"kind": "CRON", "cronExpression": r["expression"], "cronTimezone": r["timezone"]}
        else:
            raise ValueError(f"schedule {d.get('name')!r}: unknown rule kind {r['kind']!r}")
        b = d.get("bounds") or {}
        created = parse_iso_ms(d["createdAt"]) if d.get("createdAt") else self.clock.start_ms - 86_400_000
        return {
            "id": d.get("id") or f"{int(self.channel_id) * 1000 + i + 1}",
            "name": d["name"], "rule": rule, "text": d.get("text", ""),
            "bounds": {"startsAtMs": _ms(b.get("startsAt")), "untilMs": _ms(b.get("until")),
                       "maxOccurrences": b.get("maxOccurrences")},
            "paused": d.get("status", "active").lower() == "paused",
            "createdAtMs": created,
            "updatedAtMs": parse_iso_ms(d["updatedAt"]) if d.get("updatedAt") else created,
            "occurrencesUsed": d.get("occurrencesUsed"),
            "nextFireAtMs": _ms(d.get("nextFireAt")),
        }

    def _fires(self, s: dict) -> tuple[int, int | None]:
        """(occurrences used, next fire time) at the current clock."""
        if s["occurrencesUsed"] is not None:
            return int(s["occurrencesUsed"]), s["nextFireAtMs"]
        now, rule, b = self.clock.now_ms(), s["rule"], s["bounds"]
        if rule["kind"] == "AT":
            return (1, None) if rule["atMs"] <= now else (0, rule["atMs"])
        if rule["kind"] != "EVERY":
            return 0, s["nextFireAtMs"]
        step = rule["everyIntervalSeconds"] * 1000
        first = b["startsAtMs"]
        if first is None:  # schedule times are whole seconds
            first = -(-(s["createdAtMs"] + step) // 1000) * 1000
        limit = b["maxOccurrences"]
        used = 0 if now < first else (now - first) // step + 1
        nxt = first + used * step
        if b["untilMs"] is not None:
            used = min(used, 0 if b["untilMs"] < first else (b["untilMs"] - first) // step + 1)
            if nxt > b["untilMs"]:
                nxt = None
        if limit is not None and used >= limit:
            used, nxt = limit, None
        return int(used), nxt

    def wire(self, s: dict) -> dict:
        used, nxt = self._fires(s)
        if s["paused"]:
            status, nxt = "PAUSED", None
        elif nxt is None and used > 0:
            status = "COMPLETED"
        else:
            status = "ACTIVE"
        rule = {"kind": None, "atMs": None, "everyIntervalSeconds": None, "cronExpression": None,
                "cronTimezone": None}
        rule.update(s["rule"])
        return {"channel": {"id": self.channel_id}, "id": s["id"], "name": s["name"], "rule": rule,
                "bounds": dict(s["bounds"]), "message": {"text": s["text"]}, "status": status,
                "failureCode": None, "failureAtMs": None, "occurrencesUsed": used, "nextFireAtMs": nxt,
                "createdAtMs": s["createdAtMs"], "updatedAtMs": s["updatedAtMs"]}

    def snapshot(self) -> list[dict]:
        return [{"name": s["name"], "status": self.wire(s)["status"]} for s in self.items.values()]

    # ------------------------------------------------------------ GraphQL

    def execute(self, body: dict) -> tuple[str, dict]:
        """(operation name, data). Raises GraphQLError."""
        m = OPERATION.match(str(body.get("query") or ""))
        name = m.group(2) if m else "anonymous"
        v = body.get("variables") or {}
        if name == "ToolkitAgentChannel":
            return name, {"viewer": {"channels": [{"id": self.channel_id, "kind": "AGENT"}]}}
        if name == "ToolkitListAgentSchedules":
            if str(v.get("channelId")) != self.channel_id:
                return name, {"viewer": {"channel": None}}
            if v.get("after"):
                raise GraphQLError("INVALID_ARGUMENT", "invalid cursor")
            edges = [{"node": self.wire(s)} for s in self.items.values()]
            return name, {"viewer": {"channel": {"schedules": {
                "edges": edges, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        if name == "ToolkitUpdateAgentSchedule":
            return name, {"updateChannelSchedule": self.update(v.get("input") or {})}
        if name in ("ToolkitListSessionSchedules", "ToolkitUpdateSessionSchedule"):
            raise GraphQLError("NOT_FOUND", "session inbox not found")
        raise GraphQLError("NOT_FOUND", f"unknown operation {name}")

    def update(self, inp: dict) -> dict:
        if str(inp.get("channelId")) != self.channel_id:
            raise GraphQLError("NOT_FOUND", "channel not found")
        name = str(inp.get("name") or "")
        if not NAME.match(name):
            raise GraphQLError("INVALID_ARGUMENT", "name must be 1-63 lowercase alphanumeric characters or hyphens")
        now = self.clock.now_ms()
        s = self.items.get(name)
        lifecycle = inp.get("lifecycle")
        if lifecycle:
            if s is None:
                raise GraphQLError("NOT_FOUND", f"schedule {name} not found")
            if lifecycle == "REMOVED":
                del self.items[name]
                return {"schedule": None, "deleted": True}
            if lifecycle not in ("PAUSED", "ACTIVE"):
                raise GraphQLError("INVALID_ARGUMENT", f"unknown lifecycle {lifecycle}")
            s["paused"], s["updatedAtMs"] = lifecycle == "PAUSED", now
            return {"schedule": self.wire(s), "deleted": False}
        d = inp.get("definition") or {}
        rule = dict(d.get("rule") or {})
        if rule.get("kind") not in ("AT", "EVERY", "CRON"):
            raise GraphQLError("INVALID_ARGUMENT", "rule is required")
        text = str((d.get("message") or {}).get("text") or "")
        if not text.strip():
            raise GraphQLError("INVALID_ARGUMENT", "message text is required")
        b = d.get("bounds") or {}
        new = {"id": s["id"] if s else f"{int(self.channel_id) * 1000 + len(self.items) + 1}",
               "name": name, "rule": rule, "text": text,
               "bounds": {"startsAtMs": b.get("startsAtMs"), "untilMs": b.get("untilMs"),
                          "maxOccurrences": b.get("maxOccurrences")},
               "paused": False, "createdAtMs": s["createdAtMs"] if s else now, "updatedAtMs": now,
               "occurrencesUsed": None, "nextFireAtMs": None}
        if rule["kind"] == "CRON":
            new["occurrencesUsed"] = 0
        self.items[name] = new
        return {"schedule": self.wire(new), "deleted": False}
