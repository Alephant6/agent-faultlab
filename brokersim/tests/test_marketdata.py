import hashlib
import json
import os
import tempfile
import unittest

from brokersim.marketdata import MarketData, Series, load_bar_dir, parse_binance_klines, parse_daily_csv
from brokersim.util import parse_iso_ms

# Two synthetic 1m rows in Binance's 2025 spot format (microsecond timestamps).
ROWS = (
    "1749772800000000,100.0,101.0,99.0,100.5,1.0,1749772859999999,0,0,0,0,0\n"
    "1749772860000000,100.5,102.0,100.0,101.5,2.0,1749772919999999,0,0,0,0,0\n"
)


class MarketDataTest(unittest.TestCase):
    def test_binance_microseconds_become_milliseconds(self):
        bars = parse_binance_klines(ROWS)
        self.assertEqual(bars[0].open_ms, 1749772800000)
        self.assertEqual(bars[0].close_ms, 1749772859999)

    def test_price_walks_inside_bar_and_holds_between_bars(self):
        s = Series("X", "1m", parse_binance_klines(ROWS))
        t0 = 1749772800000
        self.assertAlmostEqual(s.price_at(t0), 100.0)
        self.assertAlmostEqual(s.price_at(t0 + 30_000), 100.25)
        self.assertAlmostEqual(s.price_at(t0 + 120_000 + 5), 101.5)  # after the last bar: last close
        self.assertIsNone(s.price_at(t0 - 1))

    def test_ohlcv_never_returns_a_bar_that_has_not_closed(self):
        md = MarketData()
        md.add(Series("X", "1m", parse_binance_klines(ROWS)))
        t0 = 1749772800000
        self.assertEqual(md.ohlcv("X", "1m", t0 + 59_999), [])
        self.assertEqual(len(md.ohlcv("X", "1m", t0 + 60_001)), 1)
        self.assertEqual(len(md.ohlcv("X", "1m", t0 + 10**9)), 2)

    def test_daily_csv_us_session(self):
        text = "Date,Open,High,Low,Close,Volume\n2025-06-13,200,205,199,204,1000\n"
        (bar,) = parse_daily_csv(text, "us_equity")
        self.assertEqual(bar.open_ms, parse_iso_ms("2025-06-13T13:30:00Z"))  # 09:30 EDT


class BarDirTest(unittest.TestCase):
    """The layout written by projects/alva-sim/market-data/fetch_market_data.py."""

    HEAD = "ts_ms,ts_utc,open,high,low,close,volume,trades,vwap\n"

    def _write(self, root, rel, text, listed):
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        listed[rel] = {"rows": text.count("\n") - 1, "sha256": hashlib.sha256(text.encode()).hexdigest()}

    def _dir(self, tamper=False):
        root = tempfile.mkdtemp()
        listed = {}
        self._write(root, "binance/BTCUSDT/1m.csv",
                    self.HEAD + "1749772800000,2025-06-13T00:00:00Z,100,101,99,100.5,1,10,100.2\n", listed)
        # Alpaca stamps a daily bar at midnight New York time (04:00Z in June).
        self._write(root, "alpaca/AAPL/1d.csv",
                    self.HEAD + "1749787200000,2025-06-13T04:00:00Z,199,200,195,196.45,1000,5,197\n", listed)
        self._write(root, "binance/BTCUSDT/funding_8h.csv", "ts_ms,ts_utc,funding_rate,interval_hours\n", listed)
        with open(os.path.join(root, "manifest.json"), "w") as f:
            json.dump({"files": listed}, f)
        if tamper:
            with open(os.path.join(root, "binance", "BTCUSDT", "1m.csv"), "a") as f:
                f.write("x")
        return root

    def test_loads_bars_and_puts_us_daily_bars_on_the_session(self):
        md = MarketData()
        load_bar_dir(md, self._dir())
        self.assertEqual(md.instruments(), ["AAPL", "BTCUSDT"])
        aapl = md.series["AAPL"]["1d"].bars[0]
        self.assertEqual(aapl.open_ms, parse_iso_ms("2025-06-13T13:30:00Z"))
        self.assertEqual(aapl.close_ms, parse_iso_ms("2025-06-13T20:00:00Z") - 1)
        self.assertEqual(md.series["BTCUSDT"]["1m"].bars[0].close_ms, 1749772859999)

    def test_refuses_a_file_that_does_not_match_the_manifest(self):
        with self.assertRaises(ValueError):
            load_bar_dir(MarketData(), self._dir(tamper=True))


if __name__ == "__main__":
    unittest.main()
