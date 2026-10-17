"""Deterministic replay of a recorded run.

Every request the server handled is logged to requests.jsonl, including the
intent-ids the CLI minted. Feeding the same requests to a fresh simulator
built from the same scenario must reproduce the history event for event, so a
failure found in an expensive LLM run can be re-examined for free.
"""

from __future__ import annotations

import json

from .app import App, Request
from .history import load_jsonl
from .sim import build


def replay(scenario, requests_path: str, md=None) -> tuple[list[dict], list[dict]]:
    sim = build(scenario, md=md)
    app = App(sim, control_token="replay")
    for r in load_jsonl(requests_path):
        headers = {"x-alva-api-key": sim.api_key if r.get("auth") == "ok" else "",
                   "content-type": "application/json"}
        if r.get("control"):  # advance and settle calls the harness made
            headers = {"x-sim-control": "replay", "content-type": "application/json"}
        req = Request(r["method"], r["path"], r.get("query") or {}, headers, (r.get("body") or "").encode())
        app.handle(req)
    return sim.rec.events, sim.rec.fills


def diff_histories(a: list[dict], b: list[dict]) -> list[str]:
    out = []
    for i, (x, y) in enumerate(zip(a, b)):
        if json.dumps(x, sort_keys=True) != json.dumps(y, sort_keys=True):
            out.append(f"event {i} differs:\n  recorded: {x}\n  replayed: {y}")
            if len(out) >= 5:
                break
    if len(a) != len(b):
        out.append(f"length differs: recorded {len(a)} events, replayed {len(b)}")
    return out
