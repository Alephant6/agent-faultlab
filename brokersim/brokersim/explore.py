"""Contract exploration with a scripted client, no LLM.

A client that follows Alva's broker.md to the letter (mint one intent-id,
dry-run first, retry the same id or poll after exit 2, never re-place after
exit 2, submit a new intent only after exit 1) is run against every venue
preset under every fault variant. The checker then says where even a fully
compliant client ends up with two fills. Each cell is an assumption about a
venue or about trex, not a finding about Alva, until Alva confirms the real
semantics.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field

from .app import App
from .checker import check
from .cliemu import LocalCli
from .sim import build, default_markets

PRESET_COLUMNS = ["ideal", "coinbase", "binance", "binance_a", "okx", "alpaca", "robinhood", "fix"]

PLACE = {"cmd": "order place", "live": True}


@dataclass
class Variant:
    key: str
    title: str
    faults: list = field(default_factory=list)
    trex: dict = field(default_factory=dict)
    semantics: dict = field(default_factory=dict)
    client: str = "compliant"  # compliant | compliant_retry | naive | scheduled | scheduled_fixed


VARIANTS = [
    Variant("V0", "no fault"),
    Variant("V1", "request lost before the gateway",
            [{"at": "gateway", "match": PLACE, "fault": {"kind": "drop_before"}}]),
    Variant("V2", "reply lost after the order filled (gateway)",
            [{"at": "gateway", "match": PLACE, "fault": {"kind": "drop_after"}}]),
    Variant("V2v", "venue timeout after commit; trex looks up before resending",
            [{"at": "venue", "match": {"cmd": "order place"}, "fault": {"kind": "timeout", "effect": "committed"}}]),
    Variant("V2r", "venue timeout after commit; trex resends the same id",
            [{"at": "venue", "match": {"cmd": "order place"}, "fault": {"kind": "timeout", "effect": "committed"}}],
            trex={"on_retry_after_unknown": "resend"}),
    Variant("V3", "late commit at the venue (trex unaware)",
            [{"at": "venue", "match": {"cmd": "order place"},
              "fault": {"kind": "timeout", "effect": "late", "after_calls": 3}}]),
    Variant("V3k", "late commit, trex knows it is in flight",
            [{"at": "venue", "match": {"cmd": "order place"},
              "fault": {"kind": "timeout", "effect": "late", "after_calls": 3, "trex_knows": True}}]),
    Variant("V4", "reply lost; client re-runs the command without an id",
            [{"at": "gateway", "match": PLACE, "fault": {"kind": "drop_after"}}], client="naive"),
    Variant("V7", "venue timeout + resend; duplicate id mapped to exit 1",
            [{"at": "venue", "match": {"cmd": "order place"}, "fault": {"kind": "timeout", "effect": "committed"}}],
            trex={"on_retry_after_unknown": "resend", "duplicate_reject_exit": 1}),
    Variant("V8", "scheduled message delivered twice; fresh intent-id per run",
            client="scheduled"),
    Variant("V8f", "same, intent-id derived from the message id",
            client="scheduled_fixed"),
    Variant("V11", "venue 'status unknown' + library retry with the same id",
            [{"at": "venue", "match": {"cmd": "order place"}, "fault": {"kind": "unknown", "effect": "committed"}}],
            semantics={"lib_retries": 1}),
    Variant("V13", "venue acks, then rejects",
            [{"at": "venue", "match": {"cmd": "order place"}, "fault": {"kind": "async_reject"}}]),
]

# The same faults with the other move broker.md allows after exit 2.
VARIANTS += [
    Variant(v.key + "-R", v.title + " (client retries before polling)", v.faults, v.trex, v.semantics,
            client="compliant_retry")
    for v in list(VARIANTS) if v.key in ("V2v", "V2r", "V3", "V7", "V11")
]


def scenario_for(preset: str, v: Variant) -> dict:
    markets = default_markets(preset)
    symbol = markets[0]["symbol"]
    return {
        "name": f"explore-{v.key}-{preset}",
        "venues": {preset: {"preset": preset, "semantics": dict(v.semantics), "markets": markets}},
        "accounts": [{"id": "1001", "name": f"{preset} main", "venue": preset, "paper": False,
                      "balances": {markets[0]["quote"]: 50_000}}],
        "trex": dict(v.trex),
        "faults": copy.deepcopy(v.faults),
        "expect": [{"account": "1001", "symbol": symbol, "side": "buy", "qty": 0.01}],
        "_symbol": symbol,
    }


def _place_argv(acct: str, venue: str, symbol: str, intent: str | None) -> list[str]:
    argv = ["order", "place", "--account", acct, "--venue", venue, "--symbol", symbol,
            "--side", "buy", "--amount", "0.01"]
    return argv + (["--intent-id", intent] if intent else [])


def compliant_client(cli: LocalCli, acct: str, venue: str, symbol: str, intent: str | None = None,
                     max_rounds: int = 6, strategy: str = "poll_first") -> list[str]:
    """Follow broker.md literally. Returns a short trace of decisions.

    broker.md allows two moves after exit 2: "Retry the SAME intent-id, or
    poll". ``poll_first`` looks the intent up before retrying; ``retry_first``
    retries the same id straight away and polls only on duplicate_in_flight."""
    trace = []
    env, code = cli.broker(_place_argv(acct, venue, symbol, None) + ["--dry-run"])
    if code != 0 or not env.get("admitted"):
        return ["dry-run rejected"]
    intent = intent or cli.mint()
    new_intents = 0
    for _ in range(max_rounds):
        env, code = cli.broker(_place_argv(acct, venue, symbol, intent))
        trace.append(f"place:{code}:{(env.get('reason') or {}).get('code') or env.get('status')}")
        if code == 0:
            return trace
        if code == 1:
            # broker.md: "Definitively never placed - fix the args, submit a new intent"
            if new_intents >= 1:
                return trace + ["gave up"]
            new_intents += 1
            intent = cli.mint()
            continue
        # exit 2: poll by intent-id; if nothing is found, retry the same intent-id
        if strategy == "retry_first" and (env.get("reason") or {}).get("code") != "duplicate_in_flight":
            continue
        g, gcode = cli.broker(["order", "get", "--account", acct, "--intent-id", intent])
        trace.append(f"get:{gcode}:{g.get('status') or (g.get('reason') or {}).get('code')}")
        if gcode == 0 and g.get("status") in ("closed", "open", "canceled", "expired"):
            return trace
    return trace + ["unresolved"]


def naive_client(cli: LocalCli, acct: str, venue: str, symbol: str) -> list[str]:
    """Ignores broker.md: no --intent-id; after exit 2 runs the same command again."""
    trace = []
    for _ in range(3):
        env, code = cli.broker(_place_argv(acct, venue, symbol, None))
        trace.append(f"place:{code}")
        if code != 2:
            break
    return trace


def scheduled_client(cli: LocalCli, acct: str, venue: str, symbol: str, fixed: bool) -> list[str]:
    """One consented scheduled occurrence whose message is delivered twice (V8).

    SKILL.md asks for "a fresh idempotent intent-id" per occurrence; a client
    that mints one per delivery places twice. The fix derives the id from the
    message's stable id, so the second delivery replays the first outcome."""
    trace = []
    message_id = "msg-7f3e2a"
    for delivery in range(2):
        intent = str(uuid.uuid5(uuid.NAMESPACE_URL, message_id)) if fixed else None
        trace += [f"delivery{delivery + 1}"] + compliant_client(cli, acct, venue, symbol, intent=intent)
    return trace


def settle(cli: LocalCli, acct: str, n: int = 6) -> None:
    """Let late commits and async fills land: a few harmless reads."""
    for _ in range(n):
        cli.broker(["balance", "--account", acct])


def run_case(preset: str, v: Variant, md) -> dict:
    sc = scenario_for(preset, v)
    symbol = sc.pop("_symbol")
    sim = build(sc, md=md)
    app = App(sim)
    cli = LocalCli(app, sim.api_key, seed=f"{v.key}-{preset}")
    if v.client == "naive":
        trace = naive_client(cli, "1001", preset, symbol)
    elif v.client.startswith("scheduled"):
        trace = scheduled_client(cli, "1001", preset, symbol, fixed=v.client.endswith("fixed"))
    else:
        strategy = "retry_first" if v.client == "compliant_retry" else "poll_first"
        trace = compliant_client(cli, "1001", preset, symbol, strategy=strategy)
    settle(cli, "1001")
    result = check(sim.rec.events, sim.rec.fills, sc["expect"])
    return {"preset": preset, "variant": v.key, "trace": trace, "result": result}


def cell(result: dict) -> str:
    if not result["valid"]:
        mark = "DOUBLE"
    elif result["complete"]:
        mark = "ok"
    else:
        mark = "no fill"
    if result["anomalies"]:
        mark += "*"
    return mark


def explore(md, presets: list[str] | None = None, variants: list[str] | None = None) -> list[dict]:
    presets = presets or PRESET_COLUMNS
    out = []
    for v in VARIANTS:
        if variants and v.key not in variants:
            continue
        for p in presets:
            out.append(run_case(p, v, md))
    return out


def markdown_table(results: list[dict]) -> str:
    presets = list(dict.fromkeys(r["preset"] for r in results))
    rows = {}
    for r in results:
        rows.setdefault(r["variant"], {})[r["preset"]] = cell(r["result"])
    titles = {v.key: v.title for v in VARIANTS}
    lines = ["| variant | " + " | ".join(presets) + " |", "|---|" + "---|" * len(presets)]
    for key, cells in rows.items():
        lines.append(f"| {key} {titles.get(key, '')} | " + " | ".join(cells.get(p, "") for p in presets) + " |")
    lines.append("")
    lines.append("ok = one fill; DOUBLE = two fills for one user order; no fill = nothing filled; "
                 "* = contract anomaly (false exit 1, ack-then-reject, or ambiguous lookup).")
    return "\n".join(lines)
