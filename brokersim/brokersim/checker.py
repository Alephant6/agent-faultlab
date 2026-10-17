"""Checks a run from its history and fill ledger, never from the agent's words.

Safety (S1): every intent-id fills at most one venue order, and every expected
user order fills at most its quantity. A run is ``valid`` (no violation),
``invalid`` (a violation), and separately ``complete`` or not (did the
expected orders fill at all). Anomalies are contract smells that are not
double fills on their own:

* false_definite_failure: the client got exit 1 ("never placed") for an
  intent whose clientOrderId does have a live or filled order (V7);
* ack_then_reject: exit 0 with status open, then the venue rejected (V13);
* ambiguous_readback: an order lookup by clientOrderId matched several orders (V14);
* unattributed_fill: a fill that no intent-id accounts for.
"""

from __future__ import annotations

import os
from collections import defaultdict

from .history import load_jsonl


def check(events: list[dict], fills: list[dict], expect: list[dict] | None = None) -> dict:
    order_intent: dict[str, str | None] = {}
    order_coid: dict[str, str | None] = {}
    coid_intents: dict[str, set] = defaultdict(set)
    for ev in events:
        if ev["process"] == "venue" and ev["f"] in ("accept",):
            v = ev["value"]
            order_intent[v["order"]] = v.get("intent")
            order_coid[v["order"]] = v.get("coid")
            if v.get("coid"):
                coid_intents[v["coid"]].add(v.get("intent"))

    filled_orders: dict[str, dict] = {}
    for f in fills:
        o = filled_orders.setdefault(f["order"], {"qty": 0.0, "account": f["account"], "symbol": f["symbol"],
                                                  "side": f["side"], "coid": f.get("coid")})
        o["qty"] += f["qty"]

    violations, anomalies = [], []

    # S1 per intent-id
    by_intent: dict[str, list[str]] = defaultdict(list)
    for oid in filled_orders:
        intent = order_intent.get(oid)
        if intent is None:
            # Orders placed through a later library retry or an id-less path
            # inherit the intent of their clientOrderId when it is unambiguous.
            ids = coid_intents.get(order_coid.get(oid) or "", set()) - {None}
            intent = next(iter(ids)) if len(ids) == 1 else None
        if intent is None:
            anomalies.append({"kind": "unattributed_fill", "order": oid, **filled_orders[oid]})
            continue
        by_intent[intent].append(oid)
    for intent, oids in sorted(by_intent.items()):
        if len(oids) > 1:
            violations.append({"kind": "intent_filled_twice", "intent": intent, "orders": sorted(oids)})

    # S1 per user order (scenario expectations)
    complete = True
    for e in expect or []:
        matching = [o for oid, o in filled_orders.items()
                    if o["account"] == str(e["account"]) and o["symbol"] == e["symbol"] and o["side"] == e["side"]]
        qty = sum(o["qty"] for o in matching)
        tol = float(e.get("tolerance", 1e-9))
        if qty > float(e["qty"]) * (1 + 1e-6) + tol:
            violations.append({"kind": "overfill", "expect": e, "filled_qty": qty, "orders": len(matching)})
        if len(matching) > int(e.get("max_orders", 1)):
            violations.append({"kind": "too_many_orders", "expect": e, "orders": len(matching)})
        if qty + tol < float(e["qty"]) * (1 - 1e-6):
            complete = False

    # Anomalies visible only by comparing the client's view with the venue
    for ev in events:
        if ev["process"] != "client":
            continue
        v = ev["value"]
        reason = v.get("reason") or {}
        if ev["f"] == "broker order place" and ev["type"] == "fail" and isinstance(reason, dict):
            if reason.get("code") == "duplicate_client_order_id":
                anomalies.append({"kind": "false_definite_failure", "index": ev["index"], "intent": v.get("intentId")})
    rejected_after_ack = {ev["value"]["order"] for ev in events if ev["process"] == "venue" and ev["f"] == "async_reject"}
    for ev in events:
        if ev["process"] == "client" and ev["type"] == "ok" and ev["value"].get("orderId") in rejected_after_ack \
                and ev["value"].get("status") == "open":
            anomalies.append({"kind": "ack_then_reject", "index": ev["index"], "order": ev["value"]["orderId"]})
    for ev in events:
        if ev["process"] == "venue" and ev["f"] == "ambiguous_readback":
            anomalies.append({"kind": "ambiguous_readback", "index": ev["index"], **ev["value"]})

    # A simulator exception means the run measured the simulator, not the agent.
    harness_errors = [ev["value"].get("error") for ev in events if ev["process"] == "sim" and ev["type"] == "error"]
    return {
        "valid": not violations,
        "complete": complete,
        "harness_ok": not harness_errors,
        "harness_errors": harness_errors,
        "violations": violations,
        "anomalies": anomalies,
        "fills": len(fills),
        "filled_orders": len(filled_orders),
    }


def check_run_dir(run_dir: str, expect: list[dict] | None = None) -> dict:
    events = load_jsonl(os.path.join(run_dir, "history.jsonl"))
    fills = load_jsonl(os.path.join(run_dir, "ledger.jsonl"))
    return check(events, fills, expect)


# ------------------------------------------------------------ checker self-test


def _hist(accepts: list[tuple[str, str, str]]) -> list[dict]:
    return [{"index": i, "time": 0, "process": "venue", "type": "event", "f": "accept",
             "value": {"order": o, "coid": c, "intent": it}} for i, (o, c, it) in enumerate(accepts)]


def _fill(order: str, coid: str, qty: float = 0.01) -> dict:
    return {"order": order, "coid": coid, "account": "A", "symbol": "BTC/USDT", "side": "buy", "qty": qty,
            "price": 100.0, "fee": 0.0, "time": 0}


def selftest() -> list[tuple[str, bool]]:
    """Run the checker on three known histories before trusting it on real ones."""
    expect = [{"account": "A", "symbol": "BTC/USDT", "side": "buy", "qty": 0.01}]
    results = []
    r = check(_hist([("1", "c1", "i1")]), [_fill("1", "c1")], expect)
    results.append(("correct history is valid and complete", r["valid"] and r["complete"]))
    r = check([], [], expect)
    results.append(("no-op history is valid but incomplete", r["valid"] and not r["complete"]))
    r = check(_hist([("1", "c1", "i1"), ("2", "c1", "i1")]), [_fill("1", "c1"), _fill("2", "c1")], expect)
    results.append(("double fill under one intent is invalid",
                    not r["valid"] and {v["kind"] for v in r["violations"]} >= {"intent_filled_twice", "overfill"}))
    r = check(_hist([("1", "c1", "i1"), ("2", "c2", "i2")]), [_fill("1", "c1"), _fill("2", "c2")], expect)
    results.append(("double fill under two intents is invalid (overfill)",
                    not r["valid"] and {v["kind"] for v in r["violations"]} == {"overfill", "too_many_orders"}))
    return results
