"""Simulated trading venue with clientOrderId semantics as parameters.

Real venues disagree about what a duplicate clientOrderId means: how long an id
is remembered, whether a filled order frees it, whether a duplicate returns the
original or is rejected, and whether an order can be read back by its id. Each
of those is a field of ``VenueSemantics`` (K1-K9). The
presets below are points in that space; every value is an assumption to be
checked against the venue's own documentation, and the ``source`` field says
where it came from.
"""

from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass, field, fields

from .util import DAY_MS, iso, trading_day

TERMINAL = {"closed", "canceled", "expired", "rejected"}


def us_equity_session(now: int) -> tuple[int | None, int | None]:
    """09:30-16:00 New York on now's New York date, or (None, None) on a weekend."""
    import datetime as _dt
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    day = _dt.datetime.fromtimestamp(now / 1000, ny).date()
    if day.weekday() >= 5:
        return None, None
    o = _dt.datetime.combine(day, _dt.time(9, 30), ny)
    c = _dt.datetime.combine(day, _dt.time(16, 0), ny)
    return int(o.timestamp() * 1000), int(c.timestamp() * 1000) - 1

COID_FORMATS = {
    "uuid": re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),
    "alnum32": re.compile(r"^[A-Za-z0-9]{1,32}$"),
    "max36": re.compile(r"^[.A-Za-z:/0-9_-]{1,36}$"),
    "uuid_or_18": re.compile(
        r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[ -~]{1,18})$"
    ),
    "max128": re.compile(r"^[ -~]{1,128}$"),
    "int": re.compile(r"^[0-9]{1,18}$"),
}


@dataclass
class VenueSemantics:
    # K1: how long a clientOrderId stays reserved.
    #   all | window | open_only | trading_day | none
    dedupe_scope: str = "all"
    dedupe_window_ms: int = 86_400_000
    # K2: for open_only, which statuses free the id: filled | terminal | never
    release_on: str = "terminal"
    # K3: what a detected duplicate returns: return_original | reject | report_current
    on_duplicate: str = "return_original"
    # K4: same id with different order params: conflict | as_duplicate
    on_param_mismatch: str = "conflict"
    # K6: read back by clientOrderId: yes | latest_only | scan | none
    readback: str = "yes"
    # K7: accepted clientOrderId format (keys of COID_FORMATS)
    coid_format: str = "max36"
    # K8: ack semantics: sync (final state in the response) | async (accepted, fills later)
    ack: str = "sync"
    async_fill_after_calls: int = 1
    # K9: retries inside the venue library (ccxt maxRetriesOnFailure), same params resent
    lib_retries: int = 0
    # Market model
    fee_bps: float = 10.0
    spread_bps: float = 2.0
    long_only: bool = False
    # Orders sent while a market with trading hours is closed: queue (fill at the
    # next session's open, like Alpaca's market orders) | reject (market_closed)
    closed_market: str = "queue"
    # Capability matrix shown by `describe` (mirrors broker.md's columns)
    assets: str = "crypto"
    create_order: bool = True
    options: bool = False
    testnet: bool = True
    # Provenance of the preset values (not used by the engine)
    source: str = ""

    @property
    def no_coid_lookup(self) -> bool:
        return self.readback in ("none", "scan")

    @classmethod
    def from_dict(cls, d: dict) -> "VenueSemantics":
        names = {f.name for f in fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(f"unknown venue semantics fields: {sorted(unknown)}")
        return cls(**d)


# Presets. "source" names the venue documentation each value was read from;
# values marked "assumed" have no public source yet.
PRESETS: dict[str, dict] = {
    "ideal": dict(
        dedupe_scope="all",
        on_duplicate="return_original",
        readback="yes",
        coid_format="max128",
        source="Reference point: Stripe/ACP-style idempotency (remember forever, replay the original).",
    ),
    "coinbase": dict(
        dedupe_scope="all",
        on_duplicate="return_original",
        readback="yes",
        coid_format="uuid",
        source="Coinbase Advanced Trade create-order: duplicate client_order_id returns the original order; retention period undocumented (assumed forever).",
    ),
    "binance": dict(
        dedupe_scope="open_only",
        release_on="filled",
        on_duplicate="reject",
        readback="yes",
        coid_format="max36",
        source="Binance spot REST: newClientOrderId 'can only be reused after the previous order with the same id is filled' (one of two readings); duplicate -> -2010; -1007/5XX = execution status unknown.",
    ),
    "binance_a": dict(
        dedupe_scope="open_only",
        release_on="terminal",
        on_duplicate="reject",
        readback="yes",
        coid_format="max36",
        source="Binance spot REST, other reading: newClientOrderId is unique among open orders only (any terminal state frees it).",
    ),
    "okx": dict(
        dedupe_scope="open_only",
        release_on="terminal",
        on_duplicate="reject",
        readback="latest_only",
        coid_format="alnum32",
        source="OKX v5: clOrdId up to 32 alphanumerics; duplicate -> 51016; order lookup by clOrdId returns only the latest order when ids repeat.",
    ),
    "alpaca": dict(
        dedupe_scope="all",
        on_duplicate="reject",
        readback="yes",
        coid_format="max128",
        ack="async",
        assets="us_equity+crypto",
        source="Alpaca: client_order_id up to 128 chars, must be unique (422 on reuse, assumed all-time); GET by client_order_id exists; orders are accepted then filled asynchronously.",
    ),
    "robinhood": dict(
        dedupe_scope="none",
        on_duplicate="reject",
        readback="none",
        coid_format="uuid",
        long_only=True,
        testnet=False,
        assets="us_equity",
        source="Alva broker.md: robinhood is noCoidLookup and has no paper mode. Dedupe of a reused id is undocumented (assumed none, the worst case).",
    ),
    "fix": dict(
        dedupe_scope="trading_day",
        on_duplicate="report_current",
        readback="yes",
        coid_format="max128",
        source="FIX 4.4 tag 11: ClOrdID unique within the trading day; resend marked by PossResend and answered with the current order state.",
    ),
}


def semantics_for(preset: str, overrides: dict | None = None) -> VenueSemantics:
    if preset not in PRESETS:
        raise ValueError(f"unknown venue preset {preset!r}; known: {sorted(PRESETS)}")
    d = dict(PRESETS[preset])
    d.update(overrides or {})
    return VenueSemantics.from_dict(d)


@dataclass
class Market:
    symbol: str  # venue-native symbol, e.g. 'BTC/USDT' or 'AAPL'
    instrument: str  # price series name, e.g. 'BTCUSDT'
    base: str
    quote: str
    min_amount: float = 0.0
    amount_precision: int = 8
    price_precision: int = 2
    session: str = "24x7"  # or "us_equity": 09:30-16:00 New York, trading days only


@dataclass
class Order:
    id: str
    coid: str | None
    account: str
    symbol: str
    side: str
    type: str
    amount: float
    limit_price: float | None
    created_ms: int
    status: str = "open"  # ccxt: open | closed | canceled | expired | rejected
    filled: float = 0.0
    cost: float = 0.0
    fee: float = 0.0
    updated_ms: int = 0
    reject_reason: str | None = None
    reserved: float = 0.0  # funds held while the order rests
    fill_due_call: int | None = None  # async ack: fills when the venue reaches this call
    async_outcome: str = "fill"  # or 'reject' (scheduled by a fault)
    creation_snapshot: dict = field(default_factory=dict)

    @property
    def average(self) -> float | None:
        return self.cost / self.filled if self.filled else None

    def params_key(self) -> tuple:
        return (self.symbol, self.side, self.type, round(self.amount, 12), self.limit_price)


@dataclass
class SubmitResult:
    kind: str  # new | duplicate | rejected | conflict
    order: Order | None = None
    code: str | None = None
    message: str = ""


class Venue:
    """One venue: order book for our accounts, balances, fills."""

    def __init__(self, name: str, sem: VenueSemantics, markets: list[Market], md, recorder):
        self.name = name
        self.sem = sem
        self.markets = {m.symbol: m for m in markets}
        self.md = md
        self.rec = recorder
        self.orders: dict[str, Order] = {}
        self.by_coid: dict[str, list[str]] = {}
        self.balances: dict[str, dict[str, dict[str, float]]] = {}  # account -> asset -> {free, used}
        self.cost_basis: dict[str, dict[str, list[float]]] = {}  # account -> base -> [qty, avg]
        self._seq = 0
        self.calls = 0  # venue-local call counter for async fills

    # ------------------------------------------------------------ accounts

    def open_account(self, account: str, balances: dict[str, float]) -> None:
        self.balances[account] = {a: {"free": float(v), "used": 0.0} for a, v in balances.items()}
        self.cost_basis[account] = {}

    def _bal(self, account: str, asset: str) -> dict[str, float]:
        return self.balances[account].setdefault(asset, {"free": 0.0, "used": 0.0})

    # ------------------------------------------------------------ prices

    def mid(self, symbol: str, now: int) -> float | None:
        m = self.markets.get(symbol)
        return None if m is None else self.md.price(m.instrument, now)

    def bid_ask(self, symbol: str, now: int) -> tuple[float, float] | None:
        p = self.mid(symbol, now)
        if p is None:
            return None
        half = p * self.sem.spread_bps / 20_000
        return p - half, p + half

    # ------------------------------------------------------------ trading hours

    def session_bounds(self, symbol: str, now: int) -> tuple[int, int] | None:
        """Regular session containing now's New York date, or None if that date
        is not a trading day (weekend, or no bars in data that covers it)."""
        m = self.markets[symbol]
        if m.session == "24x7":
            day = now - now % DAY_MS
            return day, day + DAY_MS - 1
        lo, hi = us_equity_session(now)
        if lo is None or self.md.traded_between(m.instrument, lo, hi) is False:
            return None
        return lo, hi

    def is_open(self, symbol: str, now: int) -> bool:
        b = self.session_bounds(symbol, now)
        return b is not None and b[0] <= now <= b[1]

    # ------------------------------------------------------------ ids

    def _next_id(self) -> str:
        self._seq += 1
        return f"{self._venue_prefix()}{self._seq:07d}"

    def _venue_prefix(self) -> str:
        # Stable, venue-looking numeric prefix (no hash randomisation).
        return str(sum(ord(c) for c in self.name) * 7919 % 90_000 + 10_000)

    def coid_ok(self, coid: str) -> bool:
        return bool(COID_FORMATS[self.sem.coid_format].match(coid))

    # ------------------------------------------------------------ dedupe

    def _holds(self, o: Order, now: int) -> bool:
        s = self.sem
        if s.dedupe_scope == "none":
            return False
        if s.dedupe_scope == "all":
            return True
        if s.dedupe_scope == "window":
            return now - o.created_ms < s.dedupe_window_ms
        if s.dedupe_scope == "trading_day":
            return trading_day(o.created_ms) == trading_day(now)
        if s.dedupe_scope == "open_only":
            if s.release_on == "never":
                return True
            if s.release_on == "filled":
                return o.status != "closed"
            return o.status not in TERMINAL
        raise ValueError(f"bad dedupe_scope {s.dedupe_scope!r}")

    def reserved_by(self, coid: str, now: int) -> Order | None:
        for oid in reversed(self.by_coid.get(coid, [])):
            o = self.orders[oid]
            if self._holds(o, now):
                return o
        return None

    def find_by_coid(self, coid: str) -> tuple[Order | None, bool]:
        """Newest order with this id, plus whether the answer was ambiguous."""
        ids = self.by_coid.get(coid, [])
        if not ids:
            return None, False
        return self.orders[ids[-1]], len(ids) > 1

    # ------------------------------------------------------------ orders

    def submit(self, req: dict, now: int, intent: str | None = None) -> SubmitResult:
        """Venue-side handling of one order request (one network attempt)."""
        coid = req.get("coid")
        m = self.markets.get(req["symbol"])
        if m is None:
            return self._reject(req, now, "unknown_symbol", f"{req['symbol']} is not listed on {self.name}")
        if coid is not None and not self.coid_ok(coid):
            return self._reject(req, now, "invalid_client_order_id", f"clientOrderId does not match {self.sem.coid_format}")
        if coid is not None:
            prior = self.reserved_by(coid, now)
            if prior is not None:
                probe = (req["symbol"], req["side"], req["type"], round(req["amount"], 12), req.get("price"))
                if probe != prior.params_key() and self.sem.on_param_mismatch == "conflict":
                    self.rec.venue("conflict", {"venue": self.name, "coid": coid, "order": prior.id})
                    return SubmitResult("conflict", prior, "client_order_id_conflict", "same clientOrderId, different parameters")
                self.rec.venue("duplicate", {"venue": self.name, "coid": coid, "order": prior.id, "behaviour": self.sem.on_duplicate})
                if self.sem.on_duplicate == "reject":
                    return SubmitResult("rejected", prior, "duplicate_client_order_id", "duplicate clientOrderId")
                return SubmitResult("duplicate", prior)
        if req["amount"] <= 0 or req["amount"] < m.min_amount:
            return self._reject(req, now, "invalid_amount", f"amount below minimum {m.min_amount}")
        ba = self.bid_ask(req["symbol"], now)
        if ba is None:
            return self._reject(req, now, "market_closed", "no price available")
        bid, ask = ba
        if self.sem.closed_market == "reject" and not self.is_open(req["symbol"], now):
            return self._reject(req, now, "market_closed", f"{req['symbol']} is outside its trading session")
        ref_price = req.get("price") if req["type"] == "limit" else (ask if req["side"] == "buy" else bid)
        acct = req["account"]
        if req["side"] == "buy":
            need = req["amount"] * ref_price * (1 + self.sem.fee_bps / 10_000)
            if self._bal(acct, m.quote)["free"] + 1e-9 < need:
                return self._reject(req, now, "insufficient_funds", f"need {need:.2f} {m.quote}")
        else:
            have = self._bal(acct, m.base)["free"]
            if have + 1e-12 < req["amount"]:
                code = "short_selling_not_allowed" if self.sem.long_only else "insufficient_funds"
                return self._reject(req, now, code, f"have {have} {m.base}")

        o = Order(
            id=self._next_id(),
            coid=coid,
            account=acct,
            symbol=req["symbol"],
            side=req["side"],
            type=req["type"],
            amount=req["amount"],
            limit_price=req.get("price"),
            created_ms=now,
            updated_ms=now,
        )
        self.orders[o.id] = o
        if coid is not None:
            self.by_coid.setdefault(coid, []).append(o.id)
        self._reserve(o, ref_price)
        self.rec.venue("accept", {"venue": self.name, "order": o.id, "coid": coid, "intent": intent,
                                  "symbol": o.symbol, "side": o.side, "amount": o.amount})
        if self.sem.ack == "async":
            o.fill_due_call = self.calls + self.sem.async_fill_after_calls
        else:
            self._try_fill(o, now)
        o.creation_snapshot = self.raw(o)
        return SubmitResult("new", o)

    def _reject(self, req: dict, now: int, code: str, msg: str) -> SubmitResult:
        self.rec.venue("reject", {"venue": self.name, "coid": req.get("coid"), "code": code, "symbol": req.get("symbol")})
        return SubmitResult("rejected", None, code, msg)

    def _reserve(self, o: Order, ref_price: float) -> None:
        m = self.markets[o.symbol]
        if o.side == "buy":
            amt = o.amount * ref_price * (1 + self.sem.fee_bps / 10_000)
            b = self._bal(o.account, m.quote)
        else:
            amt = o.amount
            b = self._bal(o.account, m.base)
        b["free"] -= amt
        b["used"] += amt
        o.reserved = amt

    def _release(self, o: Order) -> None:
        m = self.markets[o.symbol]
        b = self._bal(o.account, m.quote if o.side == "buy" else m.base)
        b["free"] += o.reserved
        b["used"] -= o.reserved
        o.reserved = 0.0

    def _try_fill(self, o: Order, now: int) -> None:
        if o.status != "open" or not self.is_open(o.symbol, now):
            return
        # An order queued while the market was closed executes at the open.
        open_ms = self.session_bounds(o.symbol, now)[0]
        queued = self.markets[o.symbol].session != "24x7" and o.created_ms < open_ms
        ba = self.bid_ask(o.symbol, open_ms if queued else now)
        if ba is None:
            return
        bid, ask = ba
        px = round(ask if o.side == "buy" else bid, self.markets[o.symbol].price_precision)
        if o.type == "limit" and o.limit_price is not None:
            if (o.side == "buy" and px > o.limit_price) or (o.side == "sell" and px < o.limit_price):
                return  # not marketable: rest
        self._fill(o, o.amount - o.filled, px, open_ms if queued else now)

    def _fill(self, o: Order, qty: float, px: float, now: int) -> None:
        m = self.markets[o.symbol]
        self._release(o)
        cost = qty * px
        fee = cost * self.sem.fee_bps / 10_000
        q = self._bal(o.account, m.quote)
        b = self._bal(o.account, m.base)
        basis = self.cost_basis[o.account].setdefault(m.base, [0.0, 0.0])
        if o.side == "buy":
            q["free"] -= cost + fee
            b["free"] += qty
            new_qty = basis[0] + qty
            basis[1] = (basis[0] * basis[1] + cost) / new_qty if new_qty else 0.0
            basis[0] = new_qty
        else:
            b["free"] -= qty
            q["free"] += cost - fee
            basis[0] = max(0.0, basis[0] - qty)
        o.filled += qty
        o.cost += cost
        o.fee += fee
        o.updated_ms = now
        if o.filled + 1e-12 >= o.amount:
            o.status = "closed"
        else:
            self._reserve(o, px)
        self.rec.fill(
            {
                "venue": self.name,
                "account": o.account,
                "order": o.id,
                "coid": o.coid,
                "symbol": o.symbol,
                "side": o.side,
                "qty": qty,
                "price": px,
                "fee": fee,
                "time": now,
            }
        )

    def on_tick(self, now: int) -> None:
        """Advance the venue by one call: async fills and resting limit orders."""
        self.calls += 1
        for o in list(self.orders.values()):
            if o.status != "open":
                continue
            if o.fill_due_call is not None:
                if self.calls < o.fill_due_call:
                    continue
                o.fill_due_call = None
                if o.async_outcome == "reject":
                    self._release(o)
                    o.status = "rejected"
                    o.reject_reason = "rejected_after_ack"
                    o.updated_ms = now
                    self.rec.venue("async_reject", {"venue": self.name, "order": o.id, "coid": o.coid})
                    continue
            self._try_fill(o, now)

    def cancel(self, oid: str, now: int) -> SubmitResult:
        o = self.orders.get(oid)
        if o is None:
            return SubmitResult("rejected", None, "order_not_found", "unknown order id")
        if o.status != "open":
            return SubmitResult("rejected", o, "not_cancelable", f"order is {o.status}")
        self._release(o)
        o.status = "canceled"
        o.updated_ms = now
        o.fill_due_call = None
        self.rec.venue("cancel", {"venue": self.name, "order": o.id, "coid": o.coid})
        return SubmitResult("new", o)

    # ------------------------------------------------------------ views (ccxt-shaped)

    def raw(self, o: Order) -> dict:
        avg = o.average
        return {
            "id": o.id,
            "clientOrderId": o.coid,
            "timestamp": o.created_ms,
            "datetime": iso(o.created_ms),
            "lastTradeTimestamp": o.updated_ms if o.filled else None,
            "status": o.status,
            "symbol": o.symbol,
            "type": o.type,
            "timeInForce": "GTC",
            "side": o.side,
            "price": o.limit_price if o.limit_price is not None else avg,
            "average": avg,
            "amount": o.amount,
            "filled": o.filled,
            "remaining": max(0.0, o.amount - o.filled),
            "cost": o.cost,
            "fee": {"cost": o.fee, "currency": self.markets[o.symbol].quote},
            "trades": [],
            "info": {"orderId": o.id, "clientOrderId": o.coid, "status": o.status.upper()},
        }

    def balance_view(self, account: str, now: int) -> dict:
        free, used, total = {}, {}, {}
        out: dict = {"info": {"account": account}, "timestamp": now, "datetime": iso(now)}
        for asset, b in sorted(self.balances.get(account, {}).items()):
            free[asset] = round(b["free"], 10)
            used[asset] = round(b["used"], 10)
            total[asset] = round(b["free"] + b["used"], 10)
            out[asset] = {"free": free[asset], "used": used[asset], "total": total[asset]}
        out.update({"free": free, "used": used, "total": total})
        return out

    def positions_view(self, account: str, now: int) -> list[dict]:
        out = []
        for m in self.markets.values():
            b = self.balances.get(account, {}).get(m.base)
            if not b:
                continue
            qty = b["free"] + b["used"]
            if qty <= 1e-12:
                continue
            mark = self.mid(m.symbol, now)
            basis = self.cost_basis.get(account, {}).get(m.base, [qty, mark or 0.0])
            out.append(
                {
                    "symbol": m.symbol,
                    "side": "long",
                    "contracts": qty,
                    "entryPrice": basis[1] or None,
                    "markPrice": mark,
                    "notional": qty * mark if mark else None,
                    "unrealizedPnl": (mark - basis[1]) * qty if mark and basis[1] else None,
                    "timestamp": now,
                    "datetime": iso(now),
                }
            )
        return out

    def state(self) -> dict:
        return {
            "semantics": asdict(self.sem),
            "orders": [dict(self.raw(o), intentHint=None) for o in self.orders.values()],
            "balances": copy.deepcopy(self.balances),
        }
