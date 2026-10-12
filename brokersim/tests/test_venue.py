import unittest

from brokersim.history import Recorder
from brokersim.marketdata import Bar, Series, synthetic_market
from brokersim.util import DAY_MS, SimClock, parse_iso_ms
from brokersim.venue import Market, Venue, semantics_for, us_equity_session

DAY0 = parse_iso_ms("2025-06-13T00:00:00Z")
MD = synthetic_market(DAY0, days=2)


def venue(preset, **overrides):
    clock = SimClock(DAY0 + 14 * 3_600_000)
    v = Venue(preset, semantics_for(preset, overrides),
              [Market("BTC/USDT", "BTCUSDT", "BTC", "USDT", 0.00001, 5, 2)], MD, Recorder(clock))
    v.open_account("A", {"USDT": 100_000})
    return v, clock


def req(coid, amount=0.01, **kw):
    r = {"account": "A", "symbol": "BTC/USDT", "side": "buy", "amount": amount, "type": "market",
         "price": None, "coid": coid}
    r.update(kw)
    return r


class DedupeTest(unittest.TestCase):
    def test_binance_reading_b_accepts_a_reused_id_after_fill(self):
        v, c = venue("binance")
        self.assertEqual(v.submit(req("x"), c.now_ms()).kind, "new")
        self.assertEqual(v.submit(req("x"), c.now_ms()).kind, "new")  # first one filled: id released
        self.assertEqual(sum(1 for o in v.orders.values() if o.status == "closed"), 2)

    def test_binance_rejects_a_reused_id_while_the_first_rests(self):
        v, c = venue("binance")
        far = 50_000.0  # buy limit far below the market: rests
        self.assertEqual(v.submit(req("x", type="limit", price=far), c.now_ms()).kind, "new")
        r = v.submit(req("x", type="limit", price=far), c.now_ms())
        self.assertEqual((r.kind, r.code), ("rejected", "duplicate_client_order_id"))

    def test_coinbase_returns_the_original(self):
        v, c = venue("coinbase")
        first = v.submit(req("6f1c0f5e-3a6b-4b8a-9a43-1c2d3e4f5a6b"), c.now_ms())
        again = v.submit(req("6f1c0f5e-3a6b-4b8a-9a43-1c2d3e4f5a6b"), c.now_ms())
        self.assertEqual(again.kind, "duplicate")
        self.assertIs(again.order, first.order)
        self.assertEqual(len(v.orders), 1)

    def test_fix_dedupes_within_the_trading_day_only(self):
        v, c = venue("fix")
        v.submit(req("abc"), c.now_ms())
        self.assertEqual(v.submit(req("abc"), c.now_ms()).kind, "duplicate")
        c.advance(DAY_MS)
        self.assertEqual(v.submit(req("abc"), c.now_ms()).kind, "new")

    def test_param_mismatch_conflicts(self):
        v, c = venue("coinbase")
        v.submit(req("6f1c0f5e-3a6b-4b8a-9a43-1c2d3e4f5a6b"), c.now_ms())
        r = v.submit(req("6f1c0f5e-3a6b-4b8a-9a43-1c2d3e4f5a6b", amount=0.02), c.now_ms())
        self.assertEqual(r.kind, "conflict")

    def test_okx_rejects_ids_outside_its_format(self):
        v, c = venue("okx")
        r = v.submit(req("not-alnum-because-dashes"), c.now_ms())
        self.assertEqual(r.code, "invalid_client_order_id")


class MatchingTest(unittest.TestCase):
    def test_insufficient_funds(self):
        v, c = venue("binance")
        r = v.submit(req("big", amount=5.0), c.now_ms())
        self.assertEqual(r.code, "insufficient_funds")

    def test_resting_limit_fills_when_price_crosses(self):
        v, c = venue("binance")
        mid = v.mid("BTC/USDT", c.now_ms())
        o = v.submit(req("lim", type="limit", price=round(mid * 1.5, 2), side="buy"), c.now_ms()).order
        self.assertEqual(o.status, "closed")  # marketable immediately
        o2 = v.submit(req("lim2", type="limit", price=round(mid * 0.5, 2)), c.now_ms()).order
        self.assertEqual(o2.status, "open")
        self.assertGreater(v.balances["A"]["USDT"]["used"], 0)
        v.cancel(o2.id, c.now_ms())
        self.assertAlmostEqual(v.balances["A"]["USDT"]["used"], 0.0)

    def test_async_ack_fills_on_a_later_tick(self):
        v, c = venue("alpaca")
        v.markets["BTC/USDT"].quote = "USDT"
        o = v.submit(req("a1"), c.now_ms()).order
        self.assertEqual(o.status, "open")
        v.on_tick(c.tick())
        self.assertEqual(o.status, "closed")


def stock_venue(**overrides):
    """AAPL with 1-minute bars on the regular sessions of 2025-06-13..20, except
    06-19 (Juneteenth). Each day opens at 100 + day of month and rises 0.01 a minute."""
    md = synthetic_market(DAY0, days=2)
    bars = []
    for dom in (13, 16, 17, 18, 20):
        lo, _ = us_equity_session(parse_iso_ms(f"2025-06-{dom}T15:00:00Z"))
        for k in range(390):
            p = 100 + dom + k * 0.01
            bars.append(Bar(lo + k * 60_000, lo + (k + 1) * 60_000 - 1, p, p + 0.01, p, p + 0.01, 1.0))
    md.add(Series("AAPL", "1m", bars))
    clock = SimClock(parse_iso_ms("2025-06-13T15:00:00Z"))
    sem = semantics_for("alpaca", dict(ack="sync", **overrides))
    v = Venue("alpaca", sem, [Market("AAPL", "AAPL", "AAPL", "USD", 0.0001, 4, 2, session="us_equity"),
                              Market("BTC/USD", "BTCUSDT", "BTC", "USD", 0.00001, 5, 2)], md, Recorder(clock))
    v.open_account("A", {"USD": 100_000})
    return v, clock


def stock(coid, symbol="AAPL", amount=1.0):
    return {"account": "A", "symbol": symbol, "side": "buy", "amount": amount, "type": "market",
            "price": None, "coid": coid}


class TradingHoursTest(unittest.TestCase):
    def test_order_in_session_fills_now(self):
        v, c = stock_venue()
        self.assertEqual(v.submit(stock("s1"), c.now_ms()).order.status, "closed")

    def test_weekend_order_waits_for_monday_open_and_fills_at_the_open(self):
        v, c = stock_venue()
        c.advance(parse_iso_ms("2025-06-14T15:00:00Z") - c.now_ms())  # Saturday
        o = v.submit(stock("s2"), c.now_ms()).order
        self.assertEqual(o.status, "open")
        v.on_tick(c.tick())
        self.assertEqual(o.status, "open")
        c.advance(parse_iso_ms("2025-06-16T15:00:00Z") - c.now_ms())  # Monday, after the open
        v.on_tick(c.tick())
        self.assertEqual(o.status, "closed")
        self.assertAlmostEqual(v.rec.fills[-1]["price"], 116.01, places=2)  # 116 at 09:30 plus half the spread

    def test_holiday_is_closed_although_it_is_a_weekday(self):
        v, c = stock_venue()
        c.advance(parse_iso_ms("2025-06-19T15:00:00Z") - c.now_ms())
        o = v.submit(stock("s3"), c.now_ms()).order
        v.on_tick(c.tick())
        self.assertEqual(o.status, "open")
        c.advance(parse_iso_ms("2025-06-20T14:00:00Z") - c.now_ms())
        v.on_tick(c.tick())
        self.assertEqual(o.status, "closed")

    def test_closed_market_can_reject_instead(self):
        v, c = stock_venue(closed_market="reject")
        c.advance(parse_iso_ms("2025-06-14T15:00:00Z") - c.now_ms())
        self.assertEqual(v.submit(stock("s4"), c.now_ms()).code, "market_closed")

    def test_crypto_trades_on_saturday(self):
        v, c = stock_venue()
        c.advance(parse_iso_ms("2025-06-14T15:00:00Z") - c.now_ms())
        self.assertEqual(v.submit(stock("s5", "BTC/USD", 0.01), c.now_ms()).order.status, "closed")


if __name__ == "__main__":
    unittest.main()
