import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "brokersim")]

from alva_gate import Gate, Journal, parse_opts  # noqa: E402
from brokersim.app import Request  # noqa: E402
from tests.helpers import ACCT, make  # noqa: E402

PLACE = ["broker", "order", "place", "--account", ACCT, "--venue", "binance", "--symbol", "BTC/USDT",
         "--side", "buy", "--amount", "0.01", "--type", "market"]
A = "aaaaaaaa-1111-4111-8111-111111111111"
B = "bbbbbbbb-2222-4222-8222-222222222222"


def late(after_calls: int) -> dict:
    return {"faults": [{"at": "venue", "match": {"cmd": "order place"},
                        "fault": {"kind": "timeout", "effect": "late", "after_calls": after_calls}}]}


class Setup:
    """brokersim in process, a gate in front of it, and a journal directory."""

    def __init__(self, scenario=None, occurrence=None):
        self.sim, self.app, self.cli = make(scenario)
        self.dir = tempfile.mkdtemp()
        self.occurrence = occurrence
        self.gate = self.new_gate()

    def run(self, argv):
        env, code = self.cli.broker(argv[1:])
        return code, json.dumps(env), ""

    def new_gate(self):  # a new process: a session that lost everything but the journal
        return Gate(self.dir, self.run, self.occurrence, pauses=(0, 0, 0, 0), sleep=lambda s: None)

    def place(self, intent=None, extra=()):
        argv = PLACE + (["--intent-id", intent] if intent else []) + list(extra)
        code, out, _ = self.gate(argv)
        return code, json.loads(out)

    def settle(self):
        self.app.handle(Request("POST", "/__sim/settle", {}, {"x-sim-control": "t"}, b"{}"))
        return len(self.sim.rec.fills)


class GateTest(unittest.TestCase):
    def test_other_commands_pass_through(self):
        s = Setup()
        argv = ["broker", "risk-rules"]
        self.assertEqual(s.gate(argv), s.run(argv))
        code, body = s.place(extra=["--dry-run"])
        self.assertEqual((code, body["status"]), (0, "admitted"))
        self.assertEqual(s.place(A, extra=["--help"])[0], 0)
        self.assertEqual(Journal(s.dir).entries(), {})

    def test_a_live_order_needs_an_intent(self):
        s = Setup()
        code, body = s.place()
        self.assertEqual((code, body["status"], body["reason"]["code"]), (1, "rejected", "gate_intent_required"))
        self.assertEqual(s.sim.rec.fills, [])

    def test_resending_after_an_unknown_outcome(self):
        # v3: the venue times out and commits three requests later; the
        # agent's lookup finds nothing and it resends the same intent.
        s = Setup(late(3))
        self.assertEqual(s.place(A)[0], 2)
        self.assertEqual(s.gate(["broker", "order", "get", "--account", ACCT, "--intent-id", A])[0], 1)
        code, body = s.place(A)
        self.assertEqual((code, body["reason"]["code"]), (1, "gate_already_placed"))
        self.assertIn("status closed", body["reason"]["message"])
        self.assertEqual(s.settle(), 1)

    def test_a_new_intent_waits_while_the_first_is_unknown(self):
        s = Setup(late(50))
        self.assertEqual(s.place(A)[0], 2)
        code, body = s.place(B)
        self.assertEqual((code, body["reason"]["code"]), (1, "gate_outcome_unknown"))
        self.assertEqual(s.settle(), 1)
        # once the first is found, it is known to be placed
        s.gate(["broker", "order", "list", "--account", ACCT, "--status", "all"])
        self.assertEqual(Journal(s.dir).entries()[A]["state"], "placed")

    def test_one_order_per_occurrence(self):
        s = Setup(occurrence="btc-daily-buy@2026-10-15T02:44:00Z")
        self.assertEqual(s.place(A)[0], 0)
        code, body = s.place(B)  # SKILL.md: a fresh intent-id for each delivery
        self.assertEqual((code, body["reason"]["code"]), (1, "gate_occurrence_already_placed"))
        self.assertEqual(s.settle(), 1)

    def test_outside_a_schedule_a_second_order_is_the_users_call(self):
        s = Setup()
        self.assertEqual(s.place(A)[0], 0)
        self.assertEqual(s.place(B)[0], 0)
        self.assertEqual(s.place(A)[1]["reason"]["code"], "gate_already_placed")
        self.assertEqual(s.settle(), 2)

    def test_replay_after_a_crash_mid_order(self):
        # The run crashes while its order is in flight; the replay starts a
        # new session, finds nothing on the account and places again.
        s = Setup(late(50), occurrence="btc-daily-buy@2026-10-15T02:44:00Z")
        self.assertEqual(s.place(A)[0], 2)
        replay = s.new_gate()
        listed = json.loads(replay(["broker", "order", "list", "--account", ACCT, "--status", "all"])[1])
        self.assertEqual(listed, [])
        code, body = s.place(B)
        self.assertEqual((code, body["reason"]["code"]), (1, "gate_outcome_unknown"))
        self.assertEqual(s.settle(), 1)

    def test_a_crash_before_the_outcome_was_written(self):
        s = Setup(late(50), occurrence="btc-daily-buy@2026-10-15T02:44:00Z")
        Journal(s.dir).write(intent=A, state="sent", occurrence=s.occurrence, venue="binance",
                             account=ACCT, symbol="BTC/USDT", side="buy")
        with open(os.path.join(s.dir, "journal.jsonl"), "a") as f:
            f.write('{"intent": "' + A + '", "sta')  # cut short by the crash
        code, body = s.place(B)
        self.assertEqual(body["reason"]["code"], "gate_outcome_unknown")
        self.assertEqual(s.sim.rec.fills, [])

    def test_a_rejected_order_does_not_block(self):
        s = Setup({"risk_rules": {"maxSingleOrder": {"enabled": True, "value": 10}}})
        self.assertEqual(s.place(A)[0], 1)
        self.assertEqual(Journal(s.dir).entries()[A]["state"], "rejected")
        self.assertNotEqual(s.place(B)[1]["reason"]["code"][:5], "gate_")


class ParseTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_opts(["--account", "1", "--intent-id=x", "--dry-run", "--side", "buy"]),
                         {"account": "1", "intent-id": "x", "dry-run": True, "side": "buy"})
        self.assertEqual(parse_opts(["--help"]), {"help": True})


if __name__ == "__main__":
    unittest.main()
