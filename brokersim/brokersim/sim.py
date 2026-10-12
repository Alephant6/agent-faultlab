"""Build a complete simulator instance from a scenario (a JSON document)."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass

from .alfs import Alfs
from .broker import Account, Broker, TrexSemantics
from .faults import Nemesis
from .history import Recorder
from .marketdata import (MarketData, Series, load_bar_dir, load_local_csv, load_manifest, parse_binance_klines,
                         parse_daily_csv)
from .util import SimClock, parse_iso_ms
from .venue import Market, Venue, semantics_for

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CRYPTO = {
    "BTC": dict(instrument="BTCUSDT", min_amount=0.00001, amount_precision=5),
    "ETH": dict(instrument="ETHUSDT", min_amount=0.0001, amount_precision=4),
}


def default_markets(preset: str) -> list[dict]:
    """Venue-native symbols per preset. USD pairs use the USDT series as a proxy."""
    if preset in ("binance", "binance_a", "okx", "ideal", "fix"):
        quote, sep = "USDT", "/"
    elif preset in ("coinbase", "alpaca"):
        quote, sep = "USD", "/"
    elif preset == "robinhood":
        quote, sep = "USD", "-"
    else:
        quote, sep = "USDT", "/"
    return [dict(symbol=f"{b}{sep}{quote}", base=b, quote=quote, **meta) for b, meta in CRYPTO.items()]


DEFAULT_SCENARIO = {
    "name": "default",
    "clock": {"start": "2025-06-13T14:30:00Z", "step_ms": 1000},
    "user": {"id": "usr_7c1e9a4f", "username": "demo_trader", "tier": "pro"},
    "api_key": "alva_local_dev_key_0000000000",
    "data": {"manifest": "data/datasets.json", "cache": "data/cache", "instruments": ["BTCUSDT", "ETHUSDT"]},
    "venues": {"binance": {"preset": "binance"}},
    "accounts": [
        {"id": "4471902", "name": "Binance spot", "venue": "binance", "exchange": "binance_spot",
         "paper": False, "identifier": "vK3x****Qp7a", "balances": {"USDT": 20000}}
    ],
    "trex": {},
    "risk_rules": {
        "maxSingleOrder": {"value": 5000, "enabled": True},
        "maxDailyTurnover": {"value": 20000, "enabled": True},
        "maxDailyOrders": {"value": 20, "enabled": True},
    },
    "faults": [],
    "fs": {},
    "expect": [],
}


def merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("venues", "fs", "trex"):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def resolve(path: str) -> str:
    if os.path.isabs(path) or os.path.exists(path):
        return path
    return os.path.join(PACKAGE_ROOT, path)


def load_scenario(path_or_dict) -> dict:
    if isinstance(path_or_dict, dict):
        sc = path_or_dict
    else:
        with open(resolve(path_or_dict), encoding="utf-8") as f:
            sc = json.load(f)
    return merge(DEFAULT_SCENARIO, sc)


def load_market_data(data: dict) -> MarketData:
    md = MarketData()
    if data.get("manifest"):
        md = load_manifest(resolve(data["manifest"]), resolve(data.get("cache", "data/cache")),
                           only=data.get("instruments"))
    for f in data.get("files", []):
        path = resolve(f["path"])
        if f.get("format", "binance-kline") == "binance-kline":
            with open(path, encoding="utf-8") as fh:
                md.add(Series(f["instrument"], f.get("interval", "1m"), parse_binance_klines(fh.read())))
        elif f["format"] == "daily-csv":
            with open(path, encoding="utf-8") as fh:
                md.add(Series(f["instrument"], "1d", parse_daily_csv(fh.read(), f.get("session", "us_equity"))))
        else:
            raise ValueError(f"unknown data format {f['format']!r}")
    if data.get("bar_dir"):
        load_bar_dir(md, resolve(data["bar_dir"]), data.get("sessions"))
    for f in data.get("local_csv", []):
        load_local_csv(md, f["instrument"], resolve(f["path"]), f.get("session", "us_equity"))
    return md


@dataclass
class Sim:
    scenario: dict
    clock: SimClock
    md: MarketData
    rec: Recorder
    nemesis: Nemesis
    broker: Broker
    alfs: Alfs

    @property
    def api_key(self) -> str:
        return self.scenario["api_key"]

    def state(self) -> dict:
        return {
            "scenario": self.scenario.get("name"),
            "broker": self.broker.state(),
            "faults": self.nemesis.summary(),
            "fs": self.alfs.snapshot(),
        }


def build(scenario, out_dir: str | None = None, md: MarketData | None = None) -> Sim:
    sc = load_scenario(scenario)
    clock = SimClock(parse_iso_ms(sc["clock"]["start"]), int(sc["clock"].get("step_ms", 1000)))
    rec = Recorder(clock, out_dir)
    if md is None:
        md = load_market_data(sc["data"])
    nemesis = Nemesis(sc.get("faults"), rec)
    venues = {}
    for name, vc in sc["venues"].items():
        preset = vc.get("preset", name)
        sem = semantics_for(preset, vc.get("semantics"))
        markets = [Market(**m) for m in (vc.get("markets") or default_markets(preset))]
        markets = [m for m in markets if m.instrument in md.series]
        venues[name] = Venue(name, sem, markets, md, rec)
    accounts = []
    for a in sc["accounts"]:
        acct = Account(id=str(a["id"]), name=a.get("name", a["id"]), venue=a["venue"], paper=bool(a.get("paper", False)),
                       identifier=a.get("identifier", "****"), created_ms=clock.start_ms - 90 * 86_400_000,
                       exchange=a.get("exchange", a["venue"]))
        venues[acct.venue].open_account(acct.id, a.get("balances", {}))
        accounts.append(acct)
    broker = Broker(clock, md, rec, nemesis, venues, accounts, TrexSemantics.from_dict(sc.get("trex")), sc["risk_rules"])
    alfs = Alfs(f"/alva/home/{sc['user']['username']}", clock)
    for path, text in (sc.get("fs") or {}).items():
        alfs.write(path, text.encode())
    rec.event("harness", "info", "start", {"scenario": sc.get("name"), "start": sc["clock"]["start"],
                                           "venues": {n: v.sem.source[:60] for n, v in venues.items()}})
    return Sim(sc, clock, md, rec, nemesis, broker, alfs)
