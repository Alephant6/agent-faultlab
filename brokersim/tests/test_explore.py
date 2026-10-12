"""Pins a few cells of the exploration matrix so semantics changes are noticed."""

import unittest

from brokersim import explore

from tests.helpers import MD

V = {v.key: v for v in explore.VARIANTS}


def cell(preset, key):
    return explore.cell(explore.run_case(preset, V[key], MD)["result"])


class ExploreTest(unittest.TestCase):
    def test_no_fault_is_one_fill_everywhere(self):
        for p in explore.PRESET_COLUMNS:
            self.assertEqual(cell(p, "V0"), "ok", p)

    def test_resend_after_fill_double_fills_only_where_fill_frees_the_id(self):
        self.assertEqual(cell("binance", "V2r-R"), "DOUBLE")
        self.assertEqual(cell("coinbase", "V2r-R"), "ok")
        self.assertEqual(cell("alpaca", "V2r-R"), "ok")

    def test_false_exit_1_leads_a_compliant_client_to_a_second_order(self):
        self.assertEqual(cell("alpaca", "V7-R"), "DOUBLE*")

    def test_scheduled_redelivery(self):
        self.assertEqual(cell("coinbase", "V8"), "DOUBLE")
        self.assertEqual(cell("coinbase", "V8f"), "ok")

    def test_naive_rerun_double_fills_even_on_the_ideal_venue(self):
        self.assertEqual(cell("ideal", "V4"), "DOUBLE")


if __name__ == "__main__":
    unittest.main()
