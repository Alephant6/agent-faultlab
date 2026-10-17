"""alva-gate: a guard between a trading agent and the `alva` CLI.

The agent keeps calling `alva` as before. Every command passes through
unchanged except a live `alva broker order place`, which is sent only when

1. no earlier live order on the same account, symbol and side still has an
   unknown outcome. The CLI's exit 2 (or a call that never returned) leaves
   the outcome unknown. Before refusing, the gate looks such an order up by
   its intent-id a few times with a growing pause; if it turns up, the
   earlier order was placed and this one is refused as a likely duplicate;
2. the intent-id has not already been placed;
3. in a scheduled run, the occurrence has not already sent a live order on
   the same account, symbol and side. The runtime names the occurrence in
   ALVA_GATE_OCCURRENCE (for example `btc-daily-buy@2026-10-15T02:44:00Z`).

Each live order is written to a journal before it is sent and again when
its outcome is known. The journal lives outside the agent's session, so a
session that crashed and was replayed still sees what the first one sent.

A refusal is printed in the CLI's own envelope with exit 1, so the agent
reads it like any rejected order: {"status": "rejected", "reason":
{"code": "gate_...", "message": ...}}. Nothing is sent to the venue.

    python3 alva_gate.py --real /path/to/alva --state DIR -- broker order place ...

Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

LOOKUP_PAUSES_S = (0, 1, 2, 4)  # before each lookup of an order whose outcome is unknown
FLAGS = {"--dry-run", "--help", "-h", "--json", "--reduce-only", "--post-only"}
SIDE_KEY = ("account", "symbol", "side")


def parse_opts(args: list[str]) -> dict:
    """`--key value`, `--key=value` and bare flags, keyed without dashes."""
    opts, i = {}, 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            key, eq, val = a.partition("=")
            name = key.lstrip("-")
            if eq:
                opts[name] = val
            elif a in FLAGS or i + 1 >= len(args) or args[i + 1].startswith("--"):
                opts[name] = True
            else:
                opts[name] = args[i + 1]
                i += 1
        i += 1
    return opts


def envelope(code: str, message: str, **detail) -> str:
    return json.dumps({"schemaVersion": 2, "status": "rejected",
                       "reason": {"code": code, "message": "alva-gate: " + message}, "gate": detail})


class Journal:
    """Append-only JSON lines; the latest line for an intent is its state."""

    def __init__(self, state_dir: str):
        os.makedirs(state_dir, exist_ok=True)
        self.path = os.path.join(state_dir, "journal.jsonl")
        self.log_path = os.path.join(state_dir, "gate.jsonl")

    def entries(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a line cut short by a crash
                    out[e["intent"]] = {**out.get(e["intent"], {}), **e}
        return out

    def write(self, **e) -> None:
        e["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(e) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def log(self, **e) -> None:
        e["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(e) + "\n")


class Gate:
    def __init__(self, state_dir: str, run, occurrence: str | None = None,
                 pauses=LOOKUP_PAUSES_S, sleep=time.sleep):
        """run(argv) -> (exit code, stdout, stderr) calls the real CLI."""
        self.journal = Journal(state_dir)
        self.run, self.occurrence, self.pauses, self.sleep = run, occurrence or None, pauses, sleep

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        if argv[:3] != ["broker", "order", "place"]:
            code, out, err = self.run(argv)
            self.observe(argv, code, out)
            return code, out, err
        o = parse_opts(argv[3:])
        if o.get("dry-run") or o.get("help") or o.get("h"):
            return self.run(argv)
        return self.place(argv, o)

    # ------------------------------------------------------------ live orders

    def place(self, argv: list[str], o: dict) -> tuple[int, str, str]:
        intent = o.get("intent-id")
        key = {k: str(o.get(k, "")) for k in SIDE_KEY}
        if not isinstance(intent, str) or not intent:
            return self.refuse(argv, "gate_intent_required",
                               "a live order needs --intent-id, so its outcome can be looked up; "
                               "run uuidgen and pass the value it prints.")
        entries = self.journal.entries()
        same = [e for e in entries.values() if all(e.get(k) == v for k, v in key.items())]
        mine = entries.get(intent)
        if mine and mine["state"] == "placed":
            return self.refuse(argv, "gate_already_placed",
                               f"intent {intent} was already placed (order {mine.get('order')}, "
                               f"status {mine.get('order_status')}); not sending it again. "
                               "Use `alva broker order get --intent-id` to follow it.", earlier=intent)
        for e in same:
            if e["state"] in ("sent", "unknown"):
                found = self.resolve(e)
                if found is None:
                    return self.refuse(argv, "gate_outcome_unknown",
                                       f"an earlier live order on this account, symbol and side (intent "
                                       f"{e['intent']}) has an unknown outcome, and {len(self.pauses)} lookups "
                                       "found nothing yet. It may still fill, so another order could buy twice. "
                                       "Tell the user the outcome is unknown; look it up again later with "
                                       "`alva broker order get --intent-id`.", earlier=e["intent"])
                if e["intent"] == intent:
                    return self.refuse(argv, "gate_already_placed",
                                       f"intent {intent}, whose outcome was unknown, turned out to be placed "
                                       f"(order {found.get('id')}, status {found.get('status')}); "
                                       "not sending it again.", earlier=intent)
                return self.refuse(argv, "gate_earlier_order_placed",
                                   f"an earlier live order on this account, symbol and side whose outcome was "
                                   f"unknown turned out to be placed: intent {e['intent']}, order {found.get('id')}, "
                                   f"status {found.get('status')}. Not sending another; tell the user.",
                                   earlier=e["intent"])
        if self.occurrence:
            for e in same:
                if e.get("occurrence") == self.occurrence and e["state"] != "rejected":
                    return self.refuse(argv, "gate_occurrence_already_placed",
                                       f"this scheduled occurrence ({self.occurrence}) already sent a live order "
                                       f"on this account, symbol and side: intent {e['intent']}, {e['state']}"
                                       f"{', order ' + str(e['order']) if e.get('order') else ''}. "
                                       "One occurrence places one order; not sending another.",
                                       earlier=e["intent"])
        self.journal.write(intent=intent, state="sent", occurrence=self.occurrence, venue=o.get("venue"), **key)
        code, out, err = self.run(argv)
        body = _json(out)
        if code == 0:
            self.journal.write(intent=intent, state="placed", order=(body or {}).get("orderId"),
                               order_status=(body or {}).get("status"))
        elif code == 1:
            self.journal.write(intent=intent, state="rejected")
        else:
            self.journal.write(intent=intent, state="unknown", exit=code)
        self.journal.log(event="sent", intent=intent, exit=code)
        return code, out, err

    def resolve(self, e: dict) -> dict | None:
        """Look an order with an unknown outcome up; the order if found."""
        lookup = ["broker", "order", "get", "--account", e["account"], "--intent-id", e["intent"]]
        if e.get("venue"):
            lookup[5:5] = ["--venue", e["venue"]]
        for pause in self.pauses:
            self.sleep(pause)
            code, out, _ = self.run(lookup)
            body = _json(out)
            self.journal.log(event="lookup", intent=e["intent"], exit=code)
            if code == 0 and isinstance(body, dict) and body.get("id"):
                self.journal.write(intent=e["intent"], state="placed", order=body.get("id"),
                                   order_status=body.get("status"))
                return body
        return None

    def observe(self, argv: list[str], code: int, out: str) -> None:
        """Learn from the agent's own lookups: a found order is placed."""
        if code != 0 or argv[:3] not in (["broker", "order", "get"], ["broker", "order", "list"]):
            return
        open_ = {i for i, e in self.journal.entries().items() if e["state"] in ("sent", "unknown")}
        if not open_:
            return
        body = _json(out)
        for order in (body if isinstance(body, list) else [body]):
            if isinstance(order, dict) and order.get("clientOrderId") in open_:
                self.journal.write(intent=order["clientOrderId"], state="placed", order=order.get("id"),
                                   order_status=order.get("status"))

    def refuse(self, argv: list[str], code: str, message: str, **detail) -> tuple[int, str, str]:
        self.journal.log(event="refused", code=code, argv=argv, **detail)
        return 1, envelope(code, message, **detail) + "\n", ""


def _json(text: str):
    try:
        return json.loads(text) if text and text.strip() else None
    except json.JSONDecodeError:
        return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--real", required=True, help="the real alva CLI")
    p.add_argument("--state", required=True, help="directory for the journal; keep it outside the agent's session")
    p.add_argument("args", nargs=argparse.REMAINDER)
    a = p.parse_args(argv)
    args = a.args[1:] if a.args[:1] == ["--"] else a.args

    def run(cli_argv: list[str]) -> tuple[int, str, str]:
        r = subprocess.run([a.real, *cli_argv], capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr

    code, out, err = Gate(a.state, run, os.environ.get("ALVA_GATE_OCCURRENCE"))(args)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


if __name__ == "__main__":
    sys.exit(main())
