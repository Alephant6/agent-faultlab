import json
import os
import tempfile
import unittest

from brokersim import checker, replay
from brokersim.app import Request

from tests.helpers import ACCT, MD, make, place

LIVE = {"cmd": "order place", "live": True}


class ThreeWayResultTest(unittest.TestCase):
    def test_dry_run_admits_without_placing(self):
        sim, app, cli = make()
        env, code = cli.broker(place(extra=["--dry-run"]))
        self.assertEqual((code, env["admitted"]), (0, True))
        self.assertEqual(sim.rec.fills, [])
        self.assertEqual(cli.stderr, [])  # the CLI does not mint for a dry run

    def test_live_order_mints_intent_and_fills(self):
        sim, app, cli = make()
        env, code = cli.broker(place())
        self.assertEqual((code, env["status"]), (0, "filled"))
        self.assertTrue(cli.stderr[0].startswith("alva broker: intent-id "))
        self.assertEqual(env["intentId"], cli.stderr[0].split()[-1])

    def test_price_collar_is_exit_1(self):
        sim, app, cli = make()
        env, code = cli.broker(place(extra=["--type", "limit", "--price", "1"]))
        self.assertEqual((code, env["status"], env["reason"]["code"]), (1, "rejected", "price_collar"))

    def test_same_intent_replays_instead_of_placing_again(self):
        sim, app, cli = make()
        cli.broker(place(intent="11111111-2222-4333-8444-555555555555"))
        env, code = cli.broker(place(intent="11111111-2222-4333-8444-555555555555"))
        self.assertEqual(code, 0)
        self.assertEqual(len(sim.rec.fills), 1)

    def test_live_order_without_any_handle_is_rejected_by_trex(self):
        sim, app, cli = make()
        env, code = cli.broker(["order", "place"], stdin={"account": ACCT, "symbol": "BTC/USDT", "side": "buy",
                                                           "amount": 0.01, "dryRun": False, "intentId": None})
        self.assertEqual(code, 1)


class FaultTest(unittest.TestCase):
    def test_drop_after_commit_is_exit_2_and_the_order_exists(self):
        sim, app, cli = make({"faults": [{"at": "gateway", "match": LIVE, "fault": {"kind": "drop_after"}}]})
        env, code = cli.broker(place(intent="aaaaaaaa-2222-4333-8444-555555555555"))
        self.assertEqual((code, env["reason"]["code"]), (2, "network"))
        self.assertEqual(len(sim.rec.fills), 1)
        got, gcode = cli.broker(["order", "get", "--account", ACCT, "--intent-id", "aaaaaaaa-2222-4333-8444-555555555555"])
        self.assertEqual((gcode, got["status"]), (0, "closed"))
        events = [e for e in sim.rec.events if e["process"] == "client" and e["f"] == "broker order place"]
        self.assertEqual([e["type"] for e in events], ["invoke", "info"])

    def test_http_429_is_exit_2_with_nothing_applied(self):
        sim, app, cli = make({"faults": [{"at": "gateway", "match": LIVE,
                                          "fault": {"kind": "http", "status": 429, "retry_after_s": 2}}]})
        env, code = cli.broker(place())
        self.assertEqual(code, 2)
        self.assertEqual(sim.rec.fills, [])

    def test_bad_api_key_is_401(self):
        sim, app, cli = make()
        cli.api_key = "alva_wrong"
        status, body = cli.rest("GET", "/api/v1/me")
        self.assertEqual((status, body["error"]["code"]), (401, "UNAUTHENTICATED"))

    def test_duplicate_in_flight_while_a_late_commit_is_pending(self):
        sim, app, cli = make({"faults": [{"at": "venue", "match": {"cmd": "order place"},
                                          "fault": {"kind": "timeout", "effect": "late", "after_calls": 3,
                                                    "trex_knows": True}}]})
        intent = "bbbbbbbb-2222-4333-8444-555555555555"
        self.assertEqual(cli.broker(place(intent=intent))[1], 2)
        env, code = cli.broker(place(intent=intent))
        self.assertEqual((code, env["reason"]["code"]), (2, "duplicate_in_flight"))
        for _ in range(3):
            cli.broker(["balance", "--account", ACCT])
        got, gcode = cli.broker(["order", "get", "--account", ACCT, "--intent-id", intent])
        self.assertEqual((gcode, got["status"]), (0, "closed"))
        self.assertEqual(len(sim.rec.fills), 1)

    def test_help_on_order_place_is_not_a_live_order(self):
        sim, app, cli = make({"faults": [{"at": "gateway", "match": LIVE, "fault": {"kind": "drop_after"}}]})
        env, code = cli.broker(place(intent="dddddddd-4444-4555-8666-777777777777", extra=["--help"]))
        self.assertEqual(code, 0)
        self.assertEqual(sim.rec.fills, [])
        invokes = [e for e in sim.rec.events if e["type"] == "invoke" and e["f"] == "broker order place"]
        self.assertFalse(invokes[-1]["value"]["live"])
        # the drop meant for the first live order is still armed
        env, code = cli.broker(place(intent="dddddddd-4444-4555-8666-888888888888"))
        self.assertEqual(code, 2)
        self.assertEqual(len(sim.rec.fills), 1)

    def test_settle_commits_a_late_order_the_client_never_waited_for(self):
        # V3: lookup finds nothing, the same intent is resent and fills; the
        # first attempt still commits once the run is settled.
        sim, app, cli = make({"faults": [{"at": "venue", "match": {"cmd": "order place"},
                                          "fault": {"kind": "timeout", "effect": "late", "after_calls": 3}}]})
        intent = "cccccccc-3333-4444-8555-666666666666"
        self.assertEqual(cli.broker(place(intent=intent))[1], 2)
        self.assertEqual(cli.broker(place(intent=intent))[1], 0)
        self.assertEqual(len(sim.rec.fills), 1)
        r = app.handle(Request("POST", "/__sim/settle", {}, {"x-sim-control": "t"}, b"{}"))
        self.assertEqual((r.status, json.loads(r.body)["committed"]), (200, 1))
        self.assertEqual(len(sim.rec.fills), 2)
        self.assertEqual(sim.broker.pending, [])

    def test_stale_quote_serves_old_timestamp(self):
        sim, app, cli = make({"faults": [{"at": "gateway", "match": {"cmd": "quote"},
                                          "fault": {"kind": "stale", "lag_ms": 3_600_000}}]})
        q, _ = cli.broker(["quote", "--venue", "binance", "--symbol", "BTC/USDT"])
        self.assertLessEqual(q["timestamp"], sim.clock.now_ms() - 3_600_000)

    def test_consent_record_read_fault(self):
        sim, app, cli = make({"fs": {"~/memory/auto-trade-consent.md": "granted_at: 2025-06-01T00:00:00Z\n"},
                              "faults": [{"at": "gateway", "match": {"route": "fs", "cmd": "read"},
                                          "fault": {"kind": "http", "status": 500}}]})
        status, _ = cli.rest("GET", "/api/v1/fs/read", {"path": "~/memory/auto-trade-consent.md"})
        self.assertEqual(status, 500)
        status, body = cli.rest("GET", "/api/v1/fs/read", {"path": "~/memory/auto-trade-consent.md"})
        self.assertEqual(status, 200)


class ControlPlaneTest(unittest.TestCase):
    def test_control_routes_hidden_without_token(self):
        sim, app, cli = make()
        r = app.handle(Request("GET", "/__sim/state", {}, {}, b""))
        self.assertEqual(r.status, 404)
        r = app.handle(Request("GET", "/__sim/state", {}, {"x-sim-control": "t"}, b""))
        self.assertEqual(r.status, 200)

    def test_responses_do_not_reveal_the_simulator(self):
        sim, app, cli = make()
        blobs = [json.dumps(cli.broker(c)[0]).lower() for c in (
            ["describe"], ["quote", "--venue", "binance", "--symbol", "BTC/USDT"], place(),
            ["balance", "--account", ACCT])]
        blobs.append(json.dumps(cli.rest("GET", "/api/v1/me")[1]).lower())
        for path in ("/api/v1/skills", "/api/v1/arrays-jwt/status", "/api/v1/trading-pairs/search"):
            blobs.append(json.dumps(cli.rest("GET", path, {"q": "BTC"})[1]).lower())
        blobs.append(json.dumps(cli.broker(place(extra=["--bogus"]))[0]).lower())
        for word in ("sim", "mock", "fake", "synthetic"):
            for b in blobs:
                self.assertNotIn(word, b)


class DeterminismTest(unittest.TestCase):
    def test_replay_reproduces_the_history(self):
        sc = {"faults": [{"at": "gateway", "match": LIVE, "fault": {"kind": "drop_after"}},
                         {"at": "venue", "match": {"cmd": "order place"}, "nth": 2,
                          "fault": {"kind": "timeout", "effect": "late", "after_calls": 2}}]}
        with tempfile.TemporaryDirectory() as d:
            from brokersim.sim import build
            from brokersim.app import App
            from brokersim.cliemu import LocalCli
            sim = build(sc, out_dir=d, md=MD)
            cli = LocalCli(App(sim), sim.api_key, seed="det")
            cli.broker(place())
            cli.broker(place())
            cli.broker(["order", "list", "--account", ACCT])
            for _ in range(3):
                cli.broker(["positions", "--account", ACCT])
            sim.rec.close()
            events, fills = replay.replay(sc, os.path.join(d, "requests.jsonl"), md=MD)
            self.assertEqual(replay.diff_histories(sim.rec.events, events), [])
            self.assertEqual(fills, sim.rec.fills)


class CheckerTest(unittest.TestCase):
    def test_selftest(self):
        for name, ok in checker.selftest():
            self.assertTrue(ok, name)


if __name__ == "__main__":
    unittest.main()
