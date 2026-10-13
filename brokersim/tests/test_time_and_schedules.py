import json
import os
import tempfile
import unittest

from brokersim import replay
from brokersim.app import App, Request
from brokersim.cliemu import LocalCli
from brokersim.sim import build
from brokersim.util import DAY_MS, parse_iso_ms, shift_times

from tests.helpers import ACCT, MD, make, place

LIST = "query ToolkitListAgentSchedules($channelId: ID!, $after: String) { viewer { ... } }"
CHANNEL = "query ToolkitAgentChannel {\n  viewer { channels { id kind } }\n}"
UPDATE = "mutation ToolkitUpdateAgentSchedule($input: UpdateChannelScheduleInput!) { ... }"

CONSENT = "granted_at: 2025-06-01T09:00:00Z\n"
DAILY = {"name": "btc-daily-buy", "rule": {"kind": "every", "interval": "P1D"},
         "bounds": {"startsAt": "2025-06-09T14:30:00Z", "maxOccurrences": 14},
         "text": "Buy 0.01 BTC.\n[auto-trade-consent: granted 2025-06-01T09:00:00Z record=~/memory/auto-trade-consent.md]",
         "createdAt": "2025-06-08T21:04:12Z"}


def gql(cli, query, variables=None):
    status, body = cli.rest("POST", "/query", body={"query": query, **({"variables": variables} if variables else {})})
    assert status == 200, (status, body)
    return body


def schedules(cli):
    body = gql(cli, LIST, {"channelId": "418207", "after": None})
    return [e["node"] for e in body["data"]["viewer"]["channel"]["schedules"]["edges"]]


class ShiftTimesTest(unittest.TestCase):
    def test_keeps_each_format(self):
        ms = 487 * DAY_MS + 5 * 3_600_000 + 3 * 60_000
        self.assertEqual(shift_times("granted_at: 2025-06-01T09:00:00Z", ms), "granted_at: 2026-10-01T14:03:00Z")
        self.assertEqual(shift_times("at 2025-06-13T14:30:00.000Z", ms), "at 2026-10-13T19:33:00.000Z")
        self.assertEqual(shift_times("2025-06-13T14:30Z", ms), "2026-10-13T19:33Z")
        self.assertEqual(shift_times("on 2025-06-13.", ms), "on 2026-10-13.")

    def test_leaves_other_numbers_alone(self):
        text = "order 2025-13-40, id 12025-06-01, v1.24.1, 4471902"
        self.assertEqual(shift_times(text, 60_000), text)

    def test_only_whole_minutes(self):
        with self.assertRaises(ValueError):
            shift_times("2025-06-13", 1000)


class MoveToTest(unittest.TestCase):
    def test_clock_files_and_prices_move_together(self):
        pinned, moved = make({"fs": {"~/memory/auto-trade-consent.md": CONSENT}}), make({
            "clock": {"move_to": "2026-10-13T05:12:40Z"}, "fs": {"~/memory/auto-trade-consent.md": CONSENT}})
        sim = moved[0]
        self.assertEqual(sim.clock.start_ms, parse_iso_ms("2026-10-13T05:12:00Z"))  # whole minutes
        self.assertEqual(sim.scenario["clock"]["pinned_start"], "2025-06-13T14:30:00Z")
        self.assertNotIn("move_to", sim.scenario["clock"])
        status, body = moved[2].rest("GET", "/api/v1/fs/read", {"path": "~/memory/auto-trade-consent.md"})
        self.assertEqual(status, 200)
        self.assertIn(b"2026-09-30T23:42:00Z", body if isinstance(body, bytes) else json.dumps(body).encode())
        # Prices move by whole days: 2026-10-13T05:12 shows the bar of 2025-06-14T05:12.
        q, _ = moved[2].broker(["quote", "--venue", "binance", "--symbol", "BTC/USDT"])
        self.assertTrue(q["datetime"].startswith("2026-10-13T05:12"))
        self.assertEqual(q["last"], round(MD.price("BTCUSDT", q["timestamp"] - 486 * DAY_MS), 2))
        pinned_q, _ = pinned[2].broker(["quote", "--venue", "binance", "--symbol", "BTC/USDT"])
        self.assertTrue(pinned_q["datetime"].startswith("2025-06-13T14:30"))

    def test_daily_bars_stay_on_midnight(self):
        moved = MD.shifted(3 * DAY_MS)
        for s in moved.series["BTCUSDT"].values():
            self.assertEqual(s.bars[0].open_ms % DAY_MS, MD.series["BTCUSDT"][s.interval].bars[0].open_ms % DAY_MS)

    def test_replay_of_a_moved_and_settled_run(self):
        sc = {"clock": {"move_to": "2026-10-13T20:01:00Z"},
              "faults": [{"at": "venue", "match": {"cmd": "order place"},
                          "fault": {"kind": "timeout", "effect": "late", "after_calls": 2}}]}
        with tempfile.TemporaryDirectory() as d:
            sim = build(sc, out_dir=d, md=MD)
            cli = LocalCli(App(sim), sim.api_key, seed="move")
            app = cli.app
            app.control_token = "t"
            cli.broker(place())
            cli.broker(["order", "list", "--account", ACCT])
            # the late commit lands only when the harness settles the run
            r = app.handle(Request("POST", "/__sim/settle", {}, {"x-sim-control": "t"}, b"{}"))
            self.assertEqual(json.loads(r.body)["committed"], 1)
            sim.rec.close()
            events, fills = replay.replay(sc, os.path.join(d, "requests.jsonl"), md=MD)
            self.assertEqual(replay.diff_histories(sim.rec.events, events), [])
            self.assertEqual(fills, sim.rec.fills)
            self.assertTrue(fills)


class SchedulesTest(unittest.TestCase):
    def test_channel_and_list_follow_the_clock(self):
        sim, app, cli = make({"schedules": [DAILY]})
        self.assertEqual(gql(cli, CHANNEL)["data"]["viewer"]["channels"], [{"id": "418207", "kind": "AGENT"}])
        [s] = schedules(cli)
        # 06-09 .. 06-13 at 14:30: the fifth occurrence is the one firing now
        self.assertEqual((s["status"], s["occurrencesUsed"]), ("ACTIVE", 5))
        self.assertEqual(s["nextFireAtMs"], parse_iso_ms("2025-06-14T14:30:00Z"))
        self.assertEqual(s["rule"]["everyIntervalSeconds"], 86400)
        self.assertEqual(s["bounds"]["maxOccurrences"], 14)
        self.assertEqual(s["nextFireAtMs"] % 1000, 0)

    def test_schedules_move_with_the_clock(self):
        sim, app, cli = make({"clock": {"move_to": "2026-10-13T05:12:00Z"}, "schedules": [DAILY]})
        [s] = schedules(cli)
        self.assertEqual(s["occurrencesUsed"], 5)
        self.assertEqual(s["nextFireAtMs"], parse_iso_ms("2026-10-14T05:12:00Z"))
        self.assertIn("granted 2026-09-30T23:42:00Z", s["message"]["text"])

    def test_bounded_schedule_completes(self):
        sim, app, cli = make({"schedules": [dict(DAILY, bounds={"startsAt": "2025-06-09T14:30:00Z",
                                                                "maxOccurrences": 3})]})
        [s] = schedules(cli)
        self.assertEqual((s["status"], s["occurrencesUsed"], s["nextFireAtMs"]), ("COMPLETED", 3, None))

    def test_pause_resume_put_delete(self):
        sim, app, cli = make({"schedules": [DAILY]})

        def update(**inp):
            return gql(cli, UPDATE, {"input": {"channelId": "418207", **inp}})

        self.assertEqual(update(name="btc-daily-buy", lifecycle="PAUSED")["data"]["updateChannelSchedule"]
                         ["schedule"]["status"], "PAUSED")
        self.assertEqual(schedules(cli)[0]["nextFireAtMs"], None)
        update(name="btc-daily-buy", lifecycle="ACTIVE")
        self.assertEqual(schedules(cli)[0]["status"], "ACTIVE")
        put = update(name="eod-report", definition={"rule": {"kind": "EVERY", "everyIntervalSeconds": 3600},
                                                    "bounds": {"maxOccurrences": 2}, "message": {"text": "Report"}})
        self.assertEqual(put["data"]["updateChannelSchedule"]["schedule"]["occurrencesUsed"], 0)
        self.assertEqual([s["name"] for s in schedules(cli)], ["btc-daily-buy", "eod-report"])
        self.assertTrue(update(name="eod-report", lifecycle="REMOVED")["data"]["updateChannelSchedule"]["deleted"])
        self.assertEqual(len(schedules(cli)), 1)
        err = update(name="eod-report", lifecycle="PAUSED")
        self.assertEqual(err["errors"][0]["extensions"]["code"], "NOT_FOUND")

    def test_other_operations_and_channels_are_not_found(self):
        sim, app, cli = make({"schedules": [DAILY]})
        body = gql(cli, "query ToolkitForYou { viewer { forYou { edges { node { id } } } } }")
        self.assertEqual(body["errors"][0]["extensions"]["code"], "NOT_FOUND")
        body = gql(cli, LIST, {"channelId": "999", "after": None})
        self.assertIsNone(body["data"]["viewer"]["channel"])
        body = gql(cli, "query ToolkitListSessionSchedules($inboxPath: String!) { x }", {"inboxPath": "/a.inbox"})
        self.assertEqual(body["errors"][0]["extensions"]["code"], "NOT_FOUND")

    def test_needs_the_api_key_and_is_recorded(self):
        sim, app, cli = make({"schedules": [DAILY]})
        r = app.handle(Request("POST", "/query", {}, {}, json.dumps({"query": CHANNEL}).encode()))
        self.assertEqual(r.status, 401)
        schedules(cli)
        self.assertIn("graphql ToolkitListAgentSchedules", [e["f"] for e in sim.rec.events if e["type"] == "invoke"])

    def test_responses_do_not_reveal_the_simulator(self):
        sim, app, cli = make({"schedules": [DAILY]})
        blobs = [json.dumps(gql(cli, q, v)).lower() for q, v in (
            (CHANNEL, None), (LIST, {"channelId": "418207"}), ("query Other { x }", None))]
        for word in ("sim", "mock", "fake", "synthetic"):
            for b in blobs:
                self.assertNotIn(word, b)


if __name__ == "__main__":
    unittest.main()
