"""Places where the simulator used to disagree with Alva's own docs (skill
v1.23.0 and the toolkit 0.31.0 types), found by running every documented
command through the real CLI. Each test names the doc rule it holds to.
"""

import json
import unittest

from brokersim import checker

from tests.helpers import ACCT, make, place

I1 = "11111111-1111-4111-8111-111111111111"
I2 = "22222222-2222-4222-8222-222222222222"


class DryRunTest(unittest.TestCase):
    def test_dry_run_returns_the_client_order_id_the_live_order_will_use(self):
        # broker.md: dry-run returns admitted:true + "the deterministic clientOrderId".
        sim, app, cli = make()
        dry, code = cli.broker(place(intent=I1, extra=["--dry-run"]))
        self.assertEqual(code, 0)
        self.assertTrue(dry["admitted"])
        self.assertEqual(dry["intentId"], I1)
        live, code = cli.broker(place(intent=I1))
        self.assertEqual(code, 0)
        self.assertEqual(dry["clientOrderId"], live["clientOrderId"])
        got, code = cli.broker(["order", "get", "--account", ACCT, "--client-order-id", dry["clientOrderId"]])
        self.assertEqual((code, got["id"]), (0, live["orderId"]))

    def test_a_refused_dry_run_is_exit_1_rejected(self):
        sim, app, cli = make()
        env, code = cli.broker(place(amount="5", intent=I1, extra=["--dry-run"]))  # ~500k USDT
        self.assertEqual((code, env["status"], env["admitted"]), (1, "rejected", False))


class FlagTest(unittest.TestCase):
    def test_a_misspelled_flag_is_refused_not_ignored(self):
        # `--dryrun` used to place and fill a live order.
        sim, app, cli = make()
        env, code = cli.broker(place(intent=I1, extra=["--dryrun"]))
        self.assertEqual((code, env["status"]), (1, "rejected"))
        self.assertIn("--dryrun", env["reason"]["message"])
        self.assertEqual(sim.rec.fills, [])
        env, code = cli.broker(place(intent=I2, extra=["--paper", "--dry-run"]))  # broker.md: there is no --paper
        self.assertEqual(code, 1)

    def test_unknown_flags_can_be_ignored_instead(self):
        sim, app, cli = make({"trex": {"unknown_flags": "ignore"}})
        env, code = cli.broker(place(intent=I1, extra=["--dryrun"]))
        self.assertEqual((code, env["status"]), (0, "filled"))

    def test_param_reaches_the_venue(self):
        # broker.md: --param key=value is "passed to the venue untouched".
        sim, app, cli = make()
        env, code = cli.broker(place(intent=I1, extra=["--param", "timeInForce=IOC"]))
        self.assertEqual(code, 0)
        self.assertEqual(env["raw"]["timeInForce"], "IOC")

    def test_an_ioc_limit_that_cannot_fill_expires(self):
        sim, app, cli = make()
        quote, _ = cli.broker(["quote", "--venue", "binance", "--symbol", "BTC/USDT"])
        far = f"{quote['bid'] * 0.95:.2f}"
        env, code = cli.broker(place(intent=I1, extra=["--type", "limit", "--price", far, "--param", "timeInForce=IOC"]))
        self.assertEqual((code, env["status"], env["filled"]), (0, "expired", 0))
        bal, _ = cli.broker(["balance", "--account", ACCT])
        self.assertEqual(bal["used"].get("USDT", 0), 0)  # nothing left reserved

    def test_post_only_that_would_take_is_rejected_by_the_venue(self):
        sim, app, cli = make()
        quote, _ = cli.broker(["quote", "--venue", "binance", "--symbol", "BTC/USDT"])
        env, code = cli.broker(place(intent=I1, extra=["--type", "limit", "--price", f"{quote['ask'] * 1.01:.2f}",
                                                       "--param", "postOnly=true"]))
        self.assertEqual((code, env["status"], env["reason"]["code"]), (1, "rejected", "post_only_would_take"))
        self.assertEqual(env["raw"]["error"]["code"], "post_only_would_take")

    def test_bad_params_are_rejected_before_anything_is_placed(self):
        sim, app, cli = make()
        env, code = cli.broker(place(intent=I1, extra=["--param", "garbage"]))
        self.assertEqual((code, env["status"]), (1, "rejected"))
        env, code = cli.broker(["order", "place", "--stdin"],
                               stdin={"account": ACCT, "venue": "binance", "symbol": "BTC/USDT", "side": "buy",
                                      "amount": 0.002, "intentId": I2, "params": ["timeInForce=IOC"]})
        self.assertEqual((code, env["status"]), (1, "rejected"))
        self.assertEqual(sim.rec.fills, [])


class BadInputTest(unittest.TestCase):
    def test_bad_arguments_are_exit_1_never_exit_2(self):
        # broker.md: exit 2 means the outcome is unknown; bad arguments are known to have done nothing.
        sim, app, cli = make()
        for argv in (["ohlcv", "--venue", "binance", "--symbol", "BTC/USDT", "--limit", "abc"],
                     ["ohlcv", "--venue", "binance", "--symbol", "BTC/USDT", "--since", "yesterday"],
                     ["order", "list", "--account", ACCT, "--limit", "x"],
                     ["order", "list", "--account", ACCT, "--status", "bogus"],
                     ["raw", "--venue", "binance", "--method", "fetchTicker", "--params", "[]"]):
            env, code = cli.broker(argv)
            self.assertEqual((code, env["status"]), (1, "rejected"), argv)
        req_body = {"argv": ["order", "place", "--stdin"], "stdin": "not json"}
        from brokersim.app import Request
        r = app.handle(Request("POST", "/api/v1/broker/invoke", {}, {"x-alva-api-key": sim.api_key},
                               json.dumps(req_body).encode()))
        out = json.loads(r.body)
        self.assertEqual(out["exit"], 1)
        self.assertIn("JSON", out["envelope"]["reason"]["message"])
        self.assertTrue(checker.check(sim.rec.events, sim.rec.fills)["harness_ok"])

    def test_exit_1_always_says_rejected(self):
        sim, app, cli = make()
        for argv in (["order", "cancel", "--id", "416800000001"],
                     ["order", "get", "--account", ACCT, "--intent-id", I1]):
            env, code = cli.broker(argv)
            self.assertEqual((code, env["status"]), (1, "rejected"), argv)


class OrderLifecycleTest(unittest.TestCase):
    def test_list_accepts_the_spine_status_words(self):
        sim, app, cli = make()
        live, _ = cli.broker(place(intent=I1))
        self.assertEqual(live["status"], "filled")
        rows, code = cli.broker(["order", "list", "--account", ACCT, "--status", "filled"])
        self.assertEqual([r["id"] for r in rows], [live["orderId"]])
        rows, _ = cli.broker(["order", "list", "--account", ACCT, "--since", str(live["ts"] + 10_000)])
        self.assertEqual(rows, [])

    def test_cancelling_a_filled_order_reports_the_fill_with_exit_0(self):
        # broker.md: exit 1 / rejected means "never placed"; this order filled.
        sim, app, cli = make()
        cli.broker(place(intent=I1))
        env, code = cli.broker(["order", "cancel", "--account", ACCT, "--intent-id", I1])
        self.assertEqual((code, env["status"], env["reason"]["code"]), (0, "filled", "not_cancelable"))
        self.assertEqual(env["intentId"], I1)


class TradingRestTest(unittest.TestCase):
    def execute(self, cli, weights, dry_run=False):
        sig = [{"date": 1749825000, "instruction": {"type": "allocate", "weights": weights}}]
        return cli.rest("POST", "/api/v1/trading/execute", body={"accountId": ACCT, "signalJson": json.dumps(sig),
                                                                 "dryRun": dry_run})

    def test_execute_obeys_the_risk_rules(self):
        # SKILL.md: live auto-trading is subject to trex risk rules (maxSingleOrder 5000 here).
        sim, app, cli = make()
        status, out = self.execute(cli, [{"symbol": "BINANCE_SPOT_BTC_USDT", "weight": 0.5}])
        self.assertEqual(status, 200)
        self.assertEqual(out["orders"][0]["status"], "rejected")
        self.assertEqual(out["orders"][0]["rejectReason"], "risk_max_single_order")
        self.assertEqual(sim.rec.fills, [])

    def test_execute_wants_canonical_symbols_for_the_account_exchange(self):
        # trading.md: canonical Arrays pairs; a mismatch with the account exchange errors.
        sim, app, cli = make()
        for sym in ("BTC/USDT", "OKX_SPOT_BTC_USDT", "binance_spot_btc_usdt"):
            status, _ = self.execute(cli, [{"symbol": sym, "weight": 0.1}], dry_run=True)
            self.assertEqual(status, 400, sym)
        status, out = self.execute(cli, [{"symbol": "BINANCE_SPOT_BTC_USDT", "weight": 0.1}])
        self.assertEqual((status, out["orders"][0]["status"]), (200, "filled"))
        _, port = cli.rest("GET", "/api/v1/trading/portfolio", {"accountId": ACCT})
        self.assertEqual([a["symbol"] for a in port["assets"]], ["BINANCE_SPOT_BTC_USDT"])

    def test_order_filters_and_equity_history(self):
        sim, app, cli = make()
        cli.broker(place(intent=I1))
        self.execute(cli, [{"symbol": "BINANCE_SPOT_ETH_USDT", "weight": 0.1}])
        _, rows = cli.rest("GET", "/api/v1/trading/orders", {"accountId": ACCT, "source": "trading"})
        self.assertEqual([r["symbol"] for r in rows], ["BINANCE_SPOT_ETH_USDT"])
        now = sim.clock.now_ms()
        _, pts = cli.rest("GET", "/api/v1/trading/equity-history",
                          {"accountId": ACCT, "timeframe": "1h", "sinceMs": now - 5 * 3_600_000, "untilMs": now})
        self.assertEqual(len(pts), 6)
        self.assertAlmostEqual(pts[0]["equity"], 20000, places=2)  # before any fill
        self.assertEqual(pts[-1]["timestamp"], now)

    def test_trading_pairs_search_finds_the_canonical_pair(self):
        # trading.md: resolve symbols with `alva trading-pairs search` and use tradingPair unchanged.
        sim, app, cli = make()
        _, out = cli.rest("GET", "/api/v1/trading-pairs/search", {"q": "btc", "strict": "true"})
        self.assertEqual([p["trading_pair"] for p in out["trading_pairs"]], ["BINANCE_SPOT_BTC_USDT"])
        status, _ = self.execute(cli, [{"symbol": out["trading_pairs"][0]["trading_pair"], "weight": 0.01}], dry_run=True)
        self.assertEqual(status, 200)

    def test_rest_accounts_have_the_toolkit_fields(self):
        sim, app, cli = make()
        _, accts = cli.rest("GET", "/api/v1/trading/accounts")
        self.assertEqual(set(accts[0]), {"id", "name", "exchange", "paper", "identifier", "createdAtMs",
                                         "subscriptions"})


class GatewayTest(unittest.TestCase):
    def test_unimplemented_routes_are_404_even_without_a_key(self):
        # A 401 here would send an agent to `alva auth login` (preflight.md).
        from brokersim.app import Request
        sim, app, cli = make()
        for path in ("/api/v1/skills", "/api/v1/trading/unsubscribe", "/api/v1/fs/rename"):
            r = app.handle(Request("GET", path, {}, {}, b""))
            self.assertEqual(r.status, 404, path)
            self.assertIn(f"GET {path}", app.unknown_routes)

    def test_token_status_has_the_toolkit_shape(self):
        sim, app, cli = make()
        _, st = cli.rest("GET", "/api/v1/arrays-jwt/status")
        self.assertEqual(set(st), {"exists", "expires_at", "tier", "renewal_needed"})
        self.assertIsInstance(st["expires_at"], int)  # epoch seconds
        self.assertFalse(st["renewal_needed"])
        _, en = cli.rest("POST", "/api/v1/arrays-jwt/ensure")
        self.assertEqual(set(en), {"expires_at", "tier", "renewed"})


if __name__ == "__main__":
    unittest.main()
