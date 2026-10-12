"""Randomised tests: malformed input never crashes the server, accounting
stays consistent under random order flows, and runs are deterministic.

Seeds are fixed, so a failure here reproduces exactly.
"""

import json
import random
import unittest

from brokersim import checker
from brokersim.app import Request

from tests.helpers import ACCT, make

PRESETS = ["ideal", "binance", "binance_a", "coinbase", "okx", "alpaca", "robinhood", "fix"]
WEIRD = ["", "-1", "0", "abc", "1e309", "nan", "inf", "-0", "0.000000001", "9" * 40, "BTC/USDT",
         "x" * 300, "é中", "--amount", "null", "[]", "{}", "true"]
COMMANDS = [["describe"], ["venues"], ["accounts"], ["risk-rules"], ["quote"], ["ohlcv"], ["balance"],
            ["positions"], ["order", "place"], ["order", "get"], ["order", "list"], ["order", "cancel"],
            ["raw"], ["nope"], ["order"], []]
FLAGS = ["--account", "--venue", "--symbol", "--side", "--amount", "--type", "--price", "--intent-id",
         "--client-order-id", "--id", "--dry-run", "--param", "--timeframe", "--since", "--limit",
         "--status", "--method", "--params", "--stdin", "--json", "--command", "--bogus"]
GOOD = {"--account": [ACCT, "999"], "--venue": ["binance", "nowhere"], "--symbol": ["BTC/USDT", "ETH/USDT", "DOGE/USDT"],
        "--side": ["buy", "sell", "hold"], "--type": ["market", "limit", "stop"], "--timeframe": ["1m", "1d", "7x"]}


def random_argv(rng):
    argv = list(rng.choice(COMMANDS))
    for _ in range(rng.randint(0, 8)):
        flag = rng.choice(FLAGS)
        argv.append(flag)
        if flag in ("--dry-run", "--stdin", "--json") and rng.random() < 0.7:
            continue
        pool = GOOD.get(flag, []) + WEIRD
        argv.append(rng.choice(pool))
    return argv


def random_request(rng, key):
    hdr = {"x-alva-api-key": key, "content-type": "application/json"}
    kind = rng.randrange(6)
    if kind == 0:
        body = {"argv": random_argv(rng)}
        if rng.random() < 0.3:
            body["stdin"] = rng.choice(['{"account": "4471902", "amount": "x"}', "not json", "[1,2]", "null",
                                        json.dumps({"account": ACCT, "symbol": "BTC/USDT", "side": "buy",
                                                    "amount": rng.choice([0.01, -5, 1e308, "1"])})])
        return Request("POST", "/api/v1/broker/invoke", {}, hdr, json.dumps(body).encode())
    if kind == 1:
        raw = rng.choice([b"", b"{", b"[]", b'{"argv": "order place"}', b'{"argv": [1, null, {}]}',
                          b'{"argv": ["order", "place"], "stdin": 5}', b"\xff\xfe", b'{"argv": []}'])
        return Request("POST", "/api/v1/broker/invoke", {}, hdr, raw)
    if kind == 2:
        path = rng.choice(["/api/v1/trading/accounts", "/api/v1/trading/portfolio", "/api/v1/trading/orders",
                           "/api/v1/trading/subscriptions", "/api/v1/trading/equity-history",
                           "/api/v1/trading/risk-rules", "/api/v1/me", "/api/v1/arrays-jwt/status"])
        q = {k: rng.choice(WEIRD + [ACCT]) for k in rng.sample(["accountId", "account_id", "limit", "status", "from", "to"], 2)}
        return Request(rng.choice(["GET", "POST", "DELETE"]), path, q, hdr, b"")
    if kind == 3:
        body = rng.choice([{}, {"accountId": ACCT}, {"accountId": ACCT, "signals": "x"},
                           {"accountId": ACCT, "signals": [{"symbol": "BTC/USDT", "weight": rng.choice([0.1, -1, 5, "a"])}]},
                           {"accountId": "nope", "signals": []}])
        return Request("POST", "/api/v1/trading/execute", {}, hdr, json.dumps(body).encode())
    if kind == 4:
        op = rng.choice(["read", "write", "stat", "readdir", "mkdir", "remove"])
        q = {"path": rng.choice(["~/memory/a.md", "/etc/passwd", "../../x", "", "~", "~/" + "d/" * 50 + "f"])}
        body = json.dumps({"path": q["path"], "content": rng.choice(["x", "y" * 100_000, 5, None])}).encode()
        return Request(rng.choice(["GET", "POST"]), f"/api/v1/fs/{op}", q, hdr, body)
    return Request(rng.choice(["GET", "POST", "PUT"]), rng.choice(["/", "/api", "/api/v1/broker", "/api/v2/x", "/__sim/state"]),
                   {}, hdr, b"{}")


class FuzzTest(unittest.TestCase):
    def test_malformed_requests_get_a_json_answer_never_a_crash(self):
        rng = random.Random(20251013)
        sim, app, cli = make()
        for i in range(3000):
            req = random_request(rng, sim.api_key)
            try:
                r = app._handle(req)
            except Exception as e:  # pragma: no cover - this is the failure being hunted
                self.fail(f"request {i} {req.method} {req.path} {req.body[:200]!r} raised {e!r}")
            self.assertLess(r.status, 500, f"request {i} {req.method} {req.path} {req.body[:200]!r}: {r.body[:300]!r}")
            if r.body:
                json.loads(r.body)
        res = checker.check(sim.rec.events, sim.rec.fills)
        self.assertTrue(res["valid"])
        self.assertTrue(res["harness_ok"], res["harness_errors"][:3])


def random_flow(preset, seed, steps=150):
    """Random order traffic from a client that follows the rules (same intent
    on retry), against one preset, with no faults."""
    rng = random.Random(f"{preset}:{seed}")
    sc = {"venues": {preset: {"preset": preset}},
          "accounts": [{"id": ACCT, "name": "a", "venue": preset, "exchange": preset, "paper": False,
                        "identifier": "x", "balances": {"USDT": 20000, "USD": 20000}}],
          "risk_rules": {"maxSingleOrder": {"value": 1e9, "enabled": True},
                         "maxDailyTurnover": {"value": 1e12, "enabled": True},
                         "maxDailyOrders": {"value": 100000, "enabled": True}}}
    sim, app, cli = make(sc)
    venue = sim.broker.venues[preset]
    symbols = list(venue.markets)
    intents = []
    exits = []
    for step in range(steps):
        r = rng.random()
        sym = rng.choice(symbols)
        if r < 0.45 or not intents:
            side = rng.choice(["buy", "sell"])
            amt = rng.choice(["0.001", "0.01", "0.05", "0.3", "2"])
            argv = ["order", "place", "--account", ACCT, "--venue", preset, "--symbol", sym, "--side", side,
                    "--amount", amt]
            if rng.random() < 0.3:
                mid = venue.mid(sym, sim.clock.now_ms()) or 1
                argv += ["--type", "limit", "--price", f"{mid * rng.choice([0.95, 0.99, 1.01, 1.05]):.2f}"]
            intent = f"{seed:08x}-0000-4000-8000-{step:012x}"
            intents.append((intent, argv))
            env, code = cli.broker(argv + ["--intent-id", intent])
        elif r < 0.6:
            intent, argv = rng.choice(intents)  # a compliant retry: same intent, same params
            env, code = cli.broker(argv + ["--intent-id", intent])
        elif r < 0.7:
            intent, _ = rng.choice(intents)
            env, code = cli.broker(["order", "cancel", "--account", ACCT, "--intent-id", intent])
        elif r < 0.8:
            intent, _ = rng.choice(intents)
            env, code = cli.broker(["order", "get", "--account", ACCT, "--intent-id", intent])
        else:
            env, code = cli.broker([rng.choice(["balance", "positions"]), "--account", ACCT])
        exits.append(code)
        assert_accounts_consistent(venue)
    return sim, venue, exits


def assert_accounts_consistent(venue):
    for acct, assets in venue.balances.items():
        for asset, b in assets.items():
            if b["free"] < -1e-6 or b["used"] < -1e-6:
                raise AssertionError(f"negative balance {acct} {asset} {b}")
        held = {}
        for o in venue.orders.values():
            if o.account == acct and o.status == "open":
                m = venue.markets[o.symbol]
                asset = m.quote if o.side == "buy" else m.base
                held[asset] = held.get(asset, 0.0) + o.reserved
        for asset, b in assets.items():
            if abs(b["used"] - held.get(asset, 0.0)) > 1e-6:
                raise AssertionError(f"used {asset}={b['used']} but open orders reserve {held.get(asset, 0.0)}")


class RandomFlowTest(unittest.TestCase):
    def test_balances_match_the_fill_ledger_on_every_preset(self):
        for preset in PRESETS:
            sim, venue, exits = random_flow(preset, seed=7)
            self.assertTrue(set(exits) <= {0, 1, 2}, (preset, set(exits)))
            totals = {}
            start = {"USDT": 20000.0, "USD": 20000.0}
            for f in sim.rec.fills:
                m = venue.markets[f["symbol"]]
                sign = 1 if f["side"] == "buy" else -1
                totals[m.base] = totals.get(m.base, 0.0) + sign * f["qty"]
                totals[m.quote] = totals.get(m.quote, 0.0) - sign * f["qty"] * f["price"] - f["fee"]
            for asset, b in venue.balances[ACCT].items():
                expected = start.get(asset, 0.0) + totals.get(asset, 0.0)
                self.assertAlmostEqual(b["free"] + b["used"], expected, places=6, msg=f"{preset} {asset}")

    def test_a_compliant_client_never_double_fills_without_faults(self):
        for preset in PRESETS:
            sim, venue, exits = random_flow(preset, seed=11)
            res = checker.check(sim.rec.events, sim.rec.fills)
            self.assertTrue(res["valid"], (preset, res["violations"][:3]))

    def test_same_seed_same_run(self):
        a, _, _ = random_flow("binance", seed=3, steps=80)
        b, _, _ = random_flow("binance", seed=3, steps=80)
        self.assertEqual(a.rec.fills, b.rec.fills)
        self.assertEqual([(e["type"], e["f"], e.get("value")) for e in a.rec.events],
                         [(e["type"], e["f"], e.get("value")) for e in b.rec.events])


class HarnessErrorTest(unittest.TestCase):
    def test_a_simulator_exception_is_a_500_and_marks_the_run_untrustworthy(self):
        sim, app, cli = make()

        def boom(*a, **k):
            raise RuntimeError("bug")

        sim.broker.invoke = boom
        env, code = cli.broker(["balance", "--account", ACCT])
        self.assertEqual(code, 2)  # the CLI sees an HTTP 500 as a network error
        res = checker.check(sim.rec.events, sim.rec.fills)
        self.assertFalse(res["harness_ok"])

    def test_restarting_serve_keeps_the_previous_run(self):
        import os
        import tempfile
        from brokersim.__main__ import _archive_previous_run

        d = tempfile.mkdtemp()
        with open(os.path.join(d, "history.jsonl"), "w") as f:
            f.write("{}\n")
        _archive_previous_run(d)
        archived = os.listdir(os.path.join(d, "archive"))
        self.assertEqual(len(archived), 1)
        self.assertTrue(os.path.exists(os.path.join(d, "archive", archived[0], "history.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(d, "history.jsonl")))


if __name__ == "__main__":
    unittest.main()
