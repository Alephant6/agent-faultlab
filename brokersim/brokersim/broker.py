"""Model of Alva's broker service ("trex") behind `alva broker`.

The CLI forwards `alva broker <argv...>` untouched to POST /api/v1/broker/invoke
and prints back ``{envelope, exit}``. This module implements that command
grammar on top of the simulated venues:

* reads (describe, accounts, quote, ohlcv, balance, positions, order get/list)
  return venue-shaped JSON;
* writes (order place / cancel) return the neutral spine from broker.md
  (status, reason, filled, price, ids, ts, raw) with the three-way exit code:
  0 done, 1 definitely not placed, 2 outcome unknown;
* a live order is keyed by its intent-id. A retry with the same id replays the
  recorded outcome, returns ``duplicate_in_flight``, or (after an unknown
  outcome) looks the order up or resends it, depending on ``TrexSemantics``.

Everything Alva's public docs do not pin down is a field of ``TrexSemantics``
with the default noted, so a run states its assumptions explicitly.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields

from .util import iso, stable_hash, trading_day
from .venue import COID_FORMATS, Order, SubmitResult, Venue

BOOL_FLAGS = {"dry-run", "help", "stdin", "json"}


@dataclass
class TrexSemantics:
    # How long an intent-id's outcome is remembered (None = forever).
    intent_retention_ms: int | None = None
    # Same intent-id retried after an exit-2 outcome:
    #   lookup: ask the venue for the clientOrderId first, resend if not found
    #   resend: resend with the same clientOrderId and let the venue dedupe
    #   replay_error: answer with the same unknown outcome again
    on_retry_after_unknown: str = "lookup"
    # Same intent-id retried while the first attempt is still in flight:
    #   duplicate_in_flight (documented in broker.md) | resend
    on_inflight_retry: str = "duplicate_in_flight"
    # Exit code for a venue "duplicate clientOrderId" rejection (V7): 1 or 2.
    duplicate_reject_exit: int = 2
    # Same intent-id with different order parameters: replay | conflict
    intent_param_mismatch: str = "replay"
    # `order get --intent-id` while the venue has not acknowledged: not_found | unknown
    inflight_get: str = "not_found"
    # intent-id -> clientOrderId: fit (hash into the venue's format) | identity
    coid_mapping: str = "fit"
    # Mark replayed write envelopes with "replayed": true (like ACP Idempotent-Replayed)
    mark_replay: bool = False
    # Admission gate (broker.md: price collar ~12%, budget/velocity/open-order limits)
    price_collar_pct: float = 12.0
    max_open_orders: int = 20
    # Flags a command does not know: reject (exit 1, never placed) | ignore.
    # Alva's docs call the grammar "the contract" but do not say which.
    unknown_flags: str = "reject"

    @classmethod
    def from_dict(cls, d: dict | None) -> "TrexSemantics":
        d = d or {}
        names = {f.name for f in fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(f"unknown trex semantics fields: {sorted(unknown)}")
        return cls(**d)


@dataclass
class Account:
    id: str
    name: str
    venue: str
    paper: bool
    identifier: str
    created_ms: int
    exchange: str


@dataclass
class IntentRecord:
    key: str
    intent_id: str | None
    coid: str
    account: str
    venue: str
    params: tuple
    created_ms: int
    state: str = "inflight"  # inflight | done | unknown
    order_id: str | None = None
    outcome: tuple | None = None  # (envelope, exit) as first answered
    attempts: int = 0


class UsageError(Exception):
    pass


def parse_argv(argv: list[str]) -> tuple[list[str], dict, list[str]]:
    """Split broker argv into command words, flags and repeated --param values."""
    words: list[str] = []
    flags: dict = {}
    params: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            name, eq, val = a[2:].partition("=")
            if eq:
                value: object = val
            elif name in BOOL_FLAGS:
                value = True
            elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                value = argv[i + 1]
                i += 1
            else:
                value = True
            if name == "param":
                params.append(str(value))
            else:
                flags[name] = value
        else:
            words.append(a)
        i += 1
    return words, flags, params


def fit_coid(raw: str, fmt: str) -> str:
    """Deterministically map an id into a venue's clientOrderId format."""
    if COID_FORMATS[fmt].match(raw):
        return raw
    h = stable_hash(raw, length=32)
    if fmt == "uuid" or fmt == "uuid_or_18":
        return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{stable_hash(raw, 'u', length=12)}"
    if fmt == "int":
        return str(int(h[:15], 16))
    return h  # 32 hex chars fit alnum32, max36 and max128


COMMANDS = [
    ("describe", "alva broker describe [--venue <venue>] [--command <command>]"),
    ("accounts", "alva broker accounts"),
    ("risk-rules", "alva broker risk-rules"),
    ("quote", "alva broker quote --venue <venue> --symbol <symbol>"),
    ("ohlcv", "alva broker ohlcv --venue <venue> --symbol <symbol> [--timeframe 1m|5m|15m|1h|4h|1d] [--since <ms>] [--limit <n>]"),
    ("balance", "alva broker balance --account <account id>"),
    ("positions", "alva broker positions --account <account id>"),
    (
        "order place",
        "alva broker order place --account <account id> --venue <venue> --symbol <symbol> --side buy|sell "
        "--amount <qty> [--type market|limit] [--price <limit price>] [--intent-id <uuid>] [--dry-run] "
        "[--param key=value]... (or --stdin with a JSON object)",
    ),
    ("order get", "alva broker order get --account <account id> (--intent-id <id> | --id <order id> | --client-order-id <id>)"),
    ("order list", "alva broker order list --account <account id> [--status open|filled|closed|canceled|expired|rejected|all] "
                   "[--symbol <symbol>] [--since <ms>] [--limit <n>]"),
    ("order cancel", "alva broker order cancel --account <account id> (--id <order id> | --intent-id <id>)"),
    ("raw", "alva broker raw --venue <venue> --method <method> --params <json>"),
]


GLOBAL_FLAGS = {"help", "json", "stdin"}
ORDER_ID_FLAGS = {"account", "venue", "id", "intent-id", "client-order-id"}
COMMAND_FLAGS = {
    "describe": {"venue", "command"},
    "venues": set(),
    "accounts": set(),
    "risk-rules": set(),
    "quote": {"venue", "symbol", "account"},
    "ohlcv": {"venue", "symbol", "account", "timeframe", "since", "limit"},
    "balance": {"account"},
    "positions": {"account"},
    "order place": {"account", "venue", "symbol", "side", "amount", "type", "price", "intent-id",
                    "client-order-id", "dry-run", "param"},
    "order get": ORDER_ID_FLAGS,
    "order list": {"account", "venue", "status", "symbol", "limit", "since"},
    "order cancel": ORDER_ID_FLAGS,
    "raw": {"venue", "method", "params"},
}
# `order list --status`: the write spine's words and ccxt's both work.
LIST_STATUS = {"open": {"open"}, "closed": {"closed"}, "filled": {"closed"}, "canceled": {"canceled"},
               "cancelled": {"canceled"}, "expired": {"expired"}, "rejected": {"rejected"}}


def unknown_flags(argv: list[str], command: str) -> list[str]:
    known = COMMAND_FLAGS.get(command)
    if known is None:
        return []
    names = [a[2:].partition("=")[0] for a in argv if a.startswith("--")]
    return [n for n in names if n not in known and n not in GLOBAL_FLAGS]


def int_flag(flags: dict, name: str, default: int | None) -> int | None:
    v = flags.get(name)
    if v is None:
        return default
    try:
        return int(str(v))
    except ValueError:
        raise UsageError(f"--{name} must be an integer") from None


class Broker:
    def __init__(self, clock, md, recorder, nemesis, venues: dict[str, Venue], accounts: list[Account],
                 trex: TrexSemantics, risk_rules: dict):
        self.clock = clock
        self.md = md
        self.rec = recorder
        self.nemesis = nemesis
        self.venues = venues
        self.accounts = {a.id: a for a in accounts}
        self.trex = trex
        self.risk_rules = risk_rules
        self.intents: dict[str, IntentRecord] = {}
        self.pending: list[dict] = []  # late venue commits
        self.calls = 0
        self.daily: dict[tuple[str, str], dict] = {}
        self.order_source: dict[str, str] = {}  # order id -> 'broker' | 'trading'

    # ================================================================ time

    def tick(self) -> None:
        """One unit of simulated progress; called once per handled request."""
        self.calls += 1
        due = [p for p in self.pending if p["due_call"] <= self.calls]
        self.pending = [p for p in self.pending if p["due_call"] > self.calls]
        self._commit_late(due)
        now = self.clock.now_ms()
        for v in self.venues.values():
            v.on_tick(now)

    def settle(self) -> int:
        """Commit every late venue commit that is still pending. The venue does
        not wait for the client to send more requests, so a run is checked
        after this. Returns how many were committed."""
        due, self.pending = self.pending, []
        self._commit_late(due)
        now = self.clock.now_ms()
        for v in self.venues.values():
            v.on_tick(now)
        return len(due)

    def _commit_late(self, due: list[dict]) -> None:
        now = self.clock.now_ms()
        for p in due:
            v = self.venues[p["venue"]]
            res = v.submit(p["req"], now, intent=p["intent_id"])
            self.rec.venue("late_commit", {"venue": v.name, "coid": p["req"].get("coid"), "intent": p["intent_id"],
                                           "result": res.kind, "order": res.order.id if res.order else None})
            rec = self.intents.get(p["key"])
            if rec is not None and p["trex_knows"]:
                env, code = self._map_submit(rec, res)
                self._finish(rec, res, env, code)
            if res.kind == "new":
                self._count_daily(rec.account if rec else p["req"]["account"], res.order)

    # ================================================================ entry

    def peek_op(self, argv: list[str], stdin: str | None) -> dict:
        """Describe a broker call for fault matching, without side effects."""
        try:
            words, flags, _ = parse_argv(argv)
        except Exception:
            return {"route": "broker", "cmd": "?"}
        body = self._stdin_obj(stdin) or {}
        cmd = " ".join(words[:2]) if words[:1] == ["order"] else (words[0] if words else "")
        intent = flags.get("intent-id") or body.get("intentId")
        coid = flags.get("client-order-id") or body.get("clientOrderId")
        dry = bool(flags.get("dry-run")) or bool(body.get("dryRun"))
        account = flags.get("account") or body.get("account")
        key = f"i:{intent}" if intent else (f"c:{coid}" if coid else None)
        acct = self.accounts.get(str(account)) if account else None
        return {
            "route": "broker",
            "cmd": cmd,
            "live": cmd == "order place" and not dry,
            "dry_run": dry,
            "venue": flags.get("venue") or body.get("venue") or (acct.venue if acct else None),
            "symbol": flags.get("symbol") or body.get("symbol"),
            "account": account,
            "intent": intent,
            "retry": key is not None and key in self.intents,
        }

    def invoke(self, argv: list[str], stdin: str | None, data_lag_ms: int = 0) -> tuple[dict, int]:
        env, code = self._invoke(argv, stdin, data_lag_ms)
        # broker.md pairs exit 1 with "rejected" and exit 2 with "error".
        if code == 1 and isinstance(env, dict) and env.get("status") == "error":
            env["status"] = "rejected"
        return env, code

    def _invoke(self, argv: list[str], stdin: str | None, data_lag_ms: int) -> tuple[dict, int]:
        try:
            words, flags, params = parse_argv(argv)
            if not words:
                return self._usage_envelope(), 0
            if flags.get("help") is True or words[0] == "help":
                return self._help(words), 0
            cmd = words[0]
            command = " ".join(words[:2]) if cmd == "order" else cmd
            bad = unknown_flags(argv, command)
            if bad and self.trex.unknown_flags == "reject":
                raise UsageError(f"unknown flag --{bad[0]} for '{command}'; see 'alva broker describe --command \"{command}\"'")
            if cmd == "order":
                sub = words[1] if len(words) > 1 else ""
                handler = {
                    "place": self.order_place,
                    "get": self.order_get,
                    "list": self.order_list,
                    "cancel": self.order_cancel,
                }.get(sub)
                if handler is None:
                    raise UsageError(f"unknown order subcommand {sub!r}")
                if sub == "place":
                    body = self._stdin_obj(stdin)
                    if flags.get("stdin") and body is None:
                        raise UsageError("--stdin needs a JSON object on standard input")
                    return handler(flags, params, body)
                return handler(flags)
            handler = {
                "describe": self.describe,
                "venues": lambda f: (self._venues_view(), 0),
                "accounts": self.accounts_cmd,
                "risk-rules": lambda f: (self.risk_rules_view(), 0),
                "quote": lambda f: self.quote(f, data_lag_ms),
                "ohlcv": lambda f: self.ohlcv(f, data_lag_ms),
                "balance": self.balance,
                "positions": self.positions,
                "raw": self.raw_cmd,
            }.get(cmd)
            if handler is None:
                raise UsageError(f"unknown broker command {cmd!r}; run 'alva broker describe'")
            return handler(flags)
        except UsageError as e:
            return self._error("invalid_argument", str(e), "rejected"), 1

    # ================================================================ helpers

    def _stdin_obj(self, stdin: str | None) -> dict | None:
        if not stdin or not isinstance(stdin, str):
            return None
        try:
            v = json.loads(stdin)
        except json.JSONDecodeError:
            return None
        return v if isinstance(v, dict) else None

    def _error(self, code: str, message: str, status: str = "error") -> dict:
        return {"schemaVersion": 2, "status": status, "reason": {"code": code, "message": message}}

    def _need(self, flags: dict, name: str) -> str:
        v = flags.get(name)
        if v is None or v is True or v == "":
            raise UsageError(f"--{name} is required")
        return str(v)

    def _account(self, flags: dict) -> Account:
        aid = self._need(flags, "account")
        acct = self.accounts.get(aid)
        if acct is None:
            raise UsageError(f"account {aid} not found; run 'alva trading accounts'")
        return acct

    def _venue_for(self, flags: dict) -> Venue:
        name = flags.get("venue")
        if not name and flags.get("account"):
            name = self._account(flags).venue
        if not name:
            raise UsageError("--venue is required")
        v = self.venues.get(str(name))
        if v is None:
            raise UsageError(f"unknown venue {name!r}; see 'alva broker describe'")
        return v

    def _count_daily(self, account: str, order: Order | None) -> None:
        if order is None:
            return
        key = (account, trading_day(self.clock.now_ms()))
        d = self.daily.setdefault(key, {"orders": 0, "turnover": 0.0})
        d["orders"] += 1
        px = order.limit_price or (self.venues[self.accounts[account].venue].mid(order.symbol, self.clock.now_ms()) or 0)
        d["turnover"] += order.amount * px

    # ================================================================ reads

    def _usage_envelope(self) -> dict:
        return {"usage": "alva broker <command> [flags]", "commands": [c for c, _ in COMMANDS],
                "nextCommands": ["alva broker describe"]}

    def _help(self, words: list[str]) -> dict:
        name = " ".join(w for w in words if w != "help")[:40]
        for c, usage in COMMANDS:
            if c == name or c.split()[0] == name:
                return {"usage": usage}
        return self._usage_envelope()

    def _venues_view(self) -> dict:
        out = []
        for v in self.venues.values():
            out.append({
                "name": v.name,
                "assets": v.sem.assets,
                "createOrder": v.sem.create_order,
                "options": v.sem.options,
                "testnet": v.sem.testnet,
                "noCoidLookup": v.sem.no_coid_lookup,
                "markets": sorted(v.markets),
            })
        return {"venues": out}

    def describe(self, flags: dict) -> tuple[dict, int]:
        if flags.get("command"):
            want = str(flags["command"])
            for c, usage in COMMANDS:
                if c == want:
                    return {"schemaVersion": 2, "command": c, "usage": usage}, 0
            return self._error("invalid_argument", f"unknown command {want!r}"), 1
        if flags.get("venue"):
            v = self.venues.get(str(flags["venue"]))
            if v is None:
                return self._error("invalid_argument", f"unknown venue {flags['venue']!r}"), 1
            view = next(x for x in self._venues_view()["venues"] if x["name"] == v.name)
            view["symbols"] = [
                {"symbol": m.symbol, "base": m.base, "quote": m.quote, "minAmount": m.min_amount,
                 "amountPrecision": m.amount_precision, "pricePrecision": m.price_precision}
                for m in v.markets.values()
            ]
            return {"schemaVersion": 2, "venue": view}, 0
        return {
            "schemaVersion": 2,
            "commands": [{"name": c, "usage": u} for c, u in COMMANDS],
            "venues": self._venues_view()["venues"],
            "nextCommands": ["alva broker describe --venue <venue>", "alva trading accounts"],
        }, 0

    def account_view(self, a: Account, rest: bool = False) -> dict:
        view = {"id": a.id, "name": a.name, "exchange": a.exchange, "venue": a.venue, "paper": a.paper,
                "identifier": a.identifier, "createdAtMs": a.created_ms, "subscriptions": []}
        if rest:
            del view["venue"]  # not a TradingAccount field in the toolkit types
        return view

    def accounts_cmd(self, flags: dict) -> tuple[dict, int]:
        return {"items": [self.account_view(a) for a in self.accounts.values()]}, 0

    def risk_rules_view(self) -> dict:
        return json.loads(json.dumps(self.risk_rules))

    def quote(self, flags: dict, lag_ms: int = 0) -> tuple[dict, int]:
        v = self._venue_for(flags)
        sym = self._need(flags, "symbol")
        m = v.markets.get(sym)
        if m is None:
            return self._error("unknown_symbol", f"{sym} is not listed on {v.name}"), 1
        t = self.clock.now_ms() - lag_ms
        ba = v.bid_ask(sym, t)
        if ba is None:
            return self._error("market_data_unavailable", f"no data for {sym}"), 1
        bid, ask = ba
        last = self.md.price(m.instrument, t)
        st = self.md.day_stats(m.instrument, t)
        return {
            "symbol": sym,
            "timestamp": t,
            "datetime": iso(t),
            "high": st["high"],
            "low": st["low"],
            "bid": round(bid, m.price_precision),
            "ask": round(ask, m.price_precision),
            "last": round(last, m.price_precision),
            "close": round(last, m.price_precision),
            "open": st["open"],
            "baseVolume": st["volume"],
            "info": {"symbol": sym.replace("/", ""), "time": t},
        }, 0

    def ohlcv(self, flags: dict, lag_ms: int = 0) -> tuple[dict, int]:
        v = self._venue_for(flags)
        sym = self._need(flags, "symbol")
        m = v.markets.get(sym)
        if m is None:
            return self._error("unknown_symbol", f"{sym} is not listed on {v.name}"), 1
        tf = str(flags.get("timeframe", "1h"))
        limit = int_flag(flags, "limit", 100)
        since = int_flag(flags, "since", None)
        try:
            rows = self.md.ohlcv(m.instrument, tf, self.clock.now_ms() - lag_ms, since, limit)
        except ValueError as e:
            return self._error("invalid_argument", str(e)), 1
        return rows, 0

    def balance(self, flags: dict) -> tuple[dict, int]:
        a = self._account(flags)
        return self.venues[a.venue].balance_view(a.id, self.clock.now_ms()), 0

    def positions(self, flags: dict) -> tuple[dict, int]:
        a = self._account(flags)
        return self.venues[a.venue].positions_view(a.id, self.clock.now_ms()), 0

    def raw_cmd(self, flags: dict) -> tuple[dict, int]:
        v = self._venue_for(flags)
        method = self._need(flags, "method")
        try:
            params = json.loads(str(flags.get("params", "{}")))
        except json.JSONDecodeError:
            params = None
        if not isinstance(params, dict):
            return self._error("invalid_argument", "--params must be a JSON object"), 1
        if method in ("fetchTicker", "fetch_ticker"):
            return self.quote({"venue": v.name, "symbol": params.get("symbol", "")})
        if method in ("fetchTime", "fetch_time"):
            return {"serverTime": self.clock.now_ms()}, 0
        return self._error("unsupported_method", f"{method} is not available on {v.name}"), 1

    # ================================================================ orders: reads

    def _resolve_order(self, flags: dict, acct: Account) -> tuple[Order | None, dict | None, int]:
        """Find an order by --id, --intent-id or --client-order-id.

        Returns (order, error_envelope, exit)."""
        v = self.venues[acct.venue]
        if flags.get("id"):
            o = v.orders.get(str(flags["id"]))
            if o is None or o.account != acct.id:
                return None, self._error("order_not_found", f"order {flags['id']} not found"), 1
            return o, None, 0
        if flags.get("intent-id"):
            rec = self.intents.get(f"i:{flags['intent-id']}")
            if rec is None:
                return None, self._error("order_not_found", f"no order for intent {flags['intent-id']}"), 1
            if rec.order_id:
                return v.orders[rec.order_id], None, 0
            if rec.state == "inflight" and self.trex.inflight_get == "unknown":
                return None, self._error("in_flight", "order outcome not known yet"), 2
            coid = rec.coid
        elif flags.get("client-order-id"):
            coid = str(flags["client-order-id"])
            rec = None
        else:
            raise UsageError("one of --id, --intent-id or --client-order-id is required")
        if v.sem.readback in ("none", "scan"):
            return None, self._error("coid_lookup_unsupported",
                                     f"{v.name} cannot look orders up by client order id"), 1
        o, ambiguous = v.find_by_coid(coid)
        if ambiguous:
            self.rec.venue("ambiguous_readback", {"venue": v.name, "coid": coid, "returned": o.id if o else None})
        if o is None:
            return None, self._error("order_not_found", f"no order with client order id {coid}"), 1
        if rec is not None and rec.order_id is None:
            rec.order_id = o.id
        return o, None, 0

    def order_get(self, flags: dict) -> tuple[dict, int]:
        acct = self._account(flags)
        o, err, code = self._resolve_order(flags, acct)
        if err is not None:
            return err, code
        return self.venues[acct.venue].raw(o), 0

    def order_list(self, flags: dict) -> tuple[list, int]:
        acct = self._account(flags)
        v = self.venues[acct.venue]
        status = str(flags.get("status", "all")).lower()
        if status != "all" and status not in LIST_STATUS:
            raise UsageError(f"--status must be one of all, {', '.join(LIST_STATUS)}")
        sym = flags.get("symbol")
        limit = int_flag(flags, "limit", 50)
        since = int_flag(flags, "since", None)
        rows = [o for o in v.orders.values() if o.account == acct.id]
        if status != "all":
            rows = [o for o in rows if o.status in LIST_STATUS[status]]
        if sym:
            rows = [o for o in rows if o.symbol == sym]
        if since is not None:
            rows = [o for o in rows if o.created_ms >= since]
        return [v.raw(o) for o in rows[-limit:]] if limit > 0 else [], 0

    # ================================================================ orders: writes

    def _spine(self, v: Venue, o: Order | None, status: str | None = None, reason: dict | None = None,
               intent: str | None = None, coid: str | None = None, replayed: bool = False) -> dict:
        if status is None and o is not None:
            status = {"closed": "filled", "canceled": "canceled", "expired": "expired",
                      "rejected": "rejected"}.get(o.status, "partial" if o.filled else "open")
        env = {
            "schemaVersion": 2,
            "status": status,
            "reason": reason,
            "filled": o.filled if o else 0,
            "price": (o.average or o.limit_price) if o else None,
            "orderId": o.id if o else None,
            "clientOrderId": coid if coid is not None else (o.coid if o else None),
            "intentId": intent,
            "ts": self.clock.now_ms(),
            "raw": v.raw(o) if o else None,
        }
        if replayed and self.trex.mark_replay:
            env["replayed"] = True
        return env

    def _order_request(self, flags: dict, params: list[str], body: dict | None) -> dict:
        body = body or {}

        def get(flag: str, key: str):
            v = flags.get(flag)
            return v if v is not None else body.get(key)

        req = {
            "account": get("account", "account"),
            "venue": get("venue", "venue"),
            "symbol": get("symbol", "symbol"),
            "side": get("side", "side"),
            "amount": get("amount", "amount"),
            "type": get("type", "type") or "market",
            "price": get("price", "price"),
            "intent_id": get("intent-id", "intentId"),
            "coid": get("client-order-id", "clientOrderId"),
            "dry_run": bool(flags.get("dry-run")) or bool(body.get("dryRun")),
            "params": {},
        }
        for p in params:
            k, eq, val = p.partition("=")
            if not eq or not k:
                raise UsageError(f"--param must be key=value, got {p!r}")
            req["params"][k] = val
        body_params = body.get("params") or {}
        if not isinstance(body_params, dict):
            raise UsageError("params must be a JSON object of venue parameters")
        req["params"].update({str(k): str(val) for k, val in body_params.items()})
        for k in ("account", "symbol", "side", "amount"):
            if req[k] in (None, True, ""):
                raise UsageError(f"--{k} is required")
        req["account"] = str(req["account"])
        try:
            req["amount"] = float(req["amount"])
            req["price"] = float(req["price"]) if req["price"] not in (None, True) else None
        except (TypeError, ValueError):
            raise UsageError("--amount and --price must be numbers")
        req["side"] = str(req["side"]).lower()
        req["type"] = str(req["type"]).lower()
        if req["side"] not in ("buy", "sell"):
            raise UsageError("--side must be buy or sell")
        if req["type"] not in ("market", "limit"):
            raise UsageError("--type must be market or limit")
        if req["type"] == "limit" and req["price"] is None:
            raise UsageError("--price is required for limit orders")
        if req["intent_id"] is not None:
            req["intent_id"] = str(req["intent_id"])
        if req["coid"] is not None:
            req["coid"] = str(req["coid"])
        return req

    def _admit(self, acct: Account, v: Venue, req: dict) -> tuple[bool, dict | None, dict]:
        now = self.clock.now_ms()
        if req["venue"] and req["venue"] != acct.venue:
            return False, {"code": "venue_mismatch", "message": f"account {acct.id} trades on {acct.venue}"}, {}
        m = v.markets.get(req["symbol"])
        if m is None:
            return False, {"code": "unknown_symbol", "message": f"{req['symbol']} is not listed on {v.name}"}, {}
        mid = v.mid(req["symbol"], now)
        if mid is None:
            return False, {"code": "market_data_unavailable", "message": "no price"}, {}
        if req["amount"] < m.min_amount or req["amount"] <= 0:
            return False, {"code": "invalid_amount", "message": f"minimum amount is {m.min_amount}"}, {}
        px = req["price"] if req["type"] == "limit" else mid
        if req["type"] == "limit" and abs(px - mid) / mid * 100 > self.trex.price_collar_pct:
            return False, {"code": "price_collar",
                           "message": f"limit price is more than {self.trex.price_collar_pct}% away from {mid:.2f}"}, {}
        notional = req["amount"] * px
        rr = self.risk_rules
        day = self.daily.get((acct.id, trading_day(now)), {"orders": 0, "turnover": 0.0})
        if rr["maxSingleOrder"]["enabled"] and notional > rr["maxSingleOrder"]["value"]:
            return False, {"code": "risk_max_single_order",
                           "message": f"order notional {notional:.2f} exceeds {rr['maxSingleOrder']['value']}"}, {}
        if rr["maxDailyOrders"]["enabled"] and day["orders"] + 1 > rr["maxDailyOrders"]["value"]:
            return False, {"code": "risk_max_daily_orders", "message": "daily order limit reached"}, {}
        if rr["maxDailyTurnover"]["enabled"] and day["turnover"] + notional > rr["maxDailyTurnover"]["value"]:
            return False, {"code": "risk_max_daily_turnover", "message": "daily turnover limit reached"}, {}
        open_orders = sum(1 for o in v.orders.values() if o.account == acct.id and o.status == "open")
        if open_orders + 1 > self.trex.max_open_orders:
            return False, {"code": "risk_max_open_orders", "message": "too many open orders"}, {}
        fee = notional * v.sem.fee_bps / 10_000
        return True, None, {"price": round(px, m.price_precision), "notional": round(notional, 2),
                            "fee": round(fee, 4), "currency": m.quote}

    def _coid_for(self, v: Venue, raw: str) -> str:
        return fit_coid(raw, v.sem.coid_format) if self.trex.coid_mapping == "fit" else raw

    def order_place(self, flags: dict, params: list[str], body: dict | None) -> tuple[dict, int]:
        try:
            req = self._order_request(flags, params, body)
        except UsageError as e:
            return self._error("invalid_argument", str(e), status="rejected"), 1
        acct = self.accounts.get(req["account"])
        if acct is None:
            return self._error("account_not_found", f"account {req['account']} not found", "rejected"), 1
        v = self.venues[acct.venue]
        now = self.clock.now_ms()

        if req["dry_run"]:
            # The clientOrderId a live run with the same handle would use. Without
            # a handle a live run is refused, so the id is only a fingerprint.
            if req["intent_id"] is not None:
                coid = req["coid"] or self._coid_for(v, req["intent_id"])
            elif req["coid"] is not None:
                coid = req["coid"]
            else:
                coid = self._coid_for(v, stable_hash(acct.id, v.name, req["symbol"], req["side"], req["amount"],
                                                     req["type"], req["price"], trading_day(now), length=24))
            ok, reason, est = self._admit(acct, v, req)
            env = self._spine(v, None, "admitted" if ok else "rejected", reason, req["intent_id"], coid)
            env.update(admitted=ok, dryRun=True, estimate=est or None)
            return env, 0 if ok else 1

        intent = req["intent_id"]
        if intent is not None:
            key, coid = f"i:{intent}", (req["coid"] or self._coid_for(v, intent))
        elif req["coid"] is not None:
            key, coid = f"c:{req['coid']}", req["coid"]
        else:
            return self._error("intent_id_required", "a live order place requires --intent-id", "rejected"), 1
        fingerprint = (acct.id, v.name, req["symbol"], req["side"], round(req["amount"], 12), req["type"], req["price"])

        rec = self.intents.get(key)
        if rec is not None and self.trex.intent_retention_ms is not None:
            if now - rec.created_ms > self.trex.intent_retention_ms:
                del self.intents[key]
                rec = None
        if rec is not None:
            return self._retry(rec, v, req, fingerprint)

        ok, reason, _ = self._admit(acct, v, req)
        rec = IntentRecord(key=key, intent_id=intent, coid=coid, account=acct.id, venue=v.name,
                           params=fingerprint, created_ms=now)
        self.intents[key] = rec
        if not ok:
            env = self._spine(v, None, "rejected", reason, intent, coid)
            rec.state, rec.outcome = "done", (env, 1)
            return env, 1
        return self._send(rec, v, req)

    def _retry(self, rec: IntentRecord, v: Venue, req: dict, fingerprint: tuple) -> tuple[dict, int]:
        if fingerprint != rec.params and self.trex.intent_param_mismatch == "conflict":
            return self._spine(v, None, "rejected", {"code": "intent_param_conflict",
                                                     "message": "intent-id reused with different parameters"},
                               rec.intent_id, rec.coid), 1
        if rec.state == "inflight":
            if self.trex.on_inflight_retry == "duplicate_in_flight":
                return self._spine(v, None, "error", {"code": "duplicate_in_flight",
                                                      "message": "an attempt with this intent-id is still in flight; poll it"},
                                   rec.intent_id, rec.coid), 2
            return self._send(rec, v, req)
        if rec.state == "done":
            env, code = rec.outcome
            env = dict(env, ts=self.clock.now_ms())
            if self.trex.mark_replay:
                env["replayed"] = True
            return env, code
        # state == unknown: the previous attempt ended with exit 2
        mode = self.trex.on_retry_after_unknown
        if mode == "replay_error":
            env, code = rec.outcome
            return dict(env, ts=self.clock.now_ms()), code
        if mode == "lookup" and v.sem.readback not in ("none", "scan"):
            o, ambiguous = v.find_by_coid(rec.coid)
            if ambiguous:
                self.rec.venue("ambiguous_readback", {"venue": v.name, "coid": rec.coid, "returned": o.id if o else None})
            if o is not None:
                env = self._spine(v, o, intent=rec.intent_id, coid=rec.coid)
                self._finish(rec, SubmitResult("duplicate", o), env, 0)
                return env, 0
        return self._send(rec, v, req)

    def _send(self, rec: IntentRecord, v: Venue, req: dict) -> tuple[dict, int]:
        """One trex attempt: up to 1 + lib_retries venue submissions."""
        rec.attempts += 1
        now = self.clock.now_ms()
        vreq = {"account": rec.account, "symbol": req["symbol"], "side": req["side"], "amount": req["amount"],
                "type": req["type"], "price": req["price"], "coid": rec.coid, "params": req["params"]}
        op = {"route": "broker", "cmd": "order place", "live": True, "venue": v.name, "symbol": req["symbol"],
              "account": rec.account, "intent": rec.intent_id, "trex_attempt": rec.attempts}
        fault = self.nemesis.pick("venue", op)
        affected = int(fault.get("attempts", 1)) if fault else 0
        unknown_code = None
        for lib_attempt in range(1 + v.sem.lib_retries):
            f = fault if lib_attempt < affected else None
            if lib_attempt > 0:
                self.rec.venue("library_retry", {"venue": v.name, "coid": rec.coid, "attempt": lib_attempt})
            if f is None:
                res = v.submit(vreq, now, intent=rec.intent_id)
                return self._settle(rec, v, res)
            kind = f["kind"]
            if kind == "reject":
                res = SubmitResult("rejected", None, f.get("code", "venue_rejected"), f.get("message", "rejected by venue"))
                return self._settle(rec, v, res)
            if kind == "async_reject":
                res = v.submit(vreq, now, intent=rec.intent_id)
                if res.kind == "new":
                    res.order.async_outcome = "reject"
                return self._settle(rec, v, res)
            # timeout / unknown: the reply is lost; the effect depends on the fault
            effect = f.get("effect", "committed")
            if effect == "committed":
                res = v.submit(vreq, now, intent=rec.intent_id)
                if res.kind == "new":
                    self._count_daily(rec.account, res.order)
            elif effect == "late":
                self.pending.append({"due_call": self.calls + int(f.get("after_calls", 3)), "venue": v.name,
                                     "req": vreq, "key": rec.key, "intent_id": rec.intent_id,
                                     "trex_knows": bool(f.get("trex_knows", False))})
            unknown_code = "venue_unknown" if kind == "unknown" else "venue_timeout"
            if effect == "late" and f.get("trex_knows", False):
                rec.state = "inflight"
                env = self._spine(v, None, "error", {"code": unknown_code, "message": "venue did not answer; outcome unknown"},
                                  rec.intent_id, rec.coid)
                rec.outcome = (env, 2)
                return env, 2
        env = self._spine(v, None, "error", {"code": unknown_code, "message": "venue did not answer; outcome unknown"},
                          rec.intent_id, rec.coid)
        rec.state, rec.outcome = "unknown", (env, 2)
        return env, 2

    def _map_submit(self, rec: IntentRecord, res: SubmitResult) -> tuple[dict, int]:
        v = self.venues[rec.venue]
        if res.kind in ("new", "duplicate"):
            o = res.order
            if res.kind == "duplicate" and v.sem.on_duplicate == "return_original":
                env = self._spine(v, o, intent=rec.intent_id, coid=rec.coid)
                snap = o.creation_snapshot or env["raw"]
                env["raw"] = snap
                env["status"] = {"closed": "filled"}.get(snap.get("status"), snap.get("status"))
                return env, 0
            return self._spine(v, o, intent=rec.intent_id, coid=rec.coid), 0
        if res.kind == "conflict":
            return self._spine(v, None, "rejected", {"code": res.code, "message": res.message},
                               rec.intent_id, rec.coid), 1
        venue_error = {"error": {"code": res.code, "message": res.message}}
        if res.code == "duplicate_client_order_id":
            code = self.trex.duplicate_reject_exit
            status = "rejected" if code == 1 else "error"
            env = self._spine(v, None, status, {"code": res.code, "message": res.message}, rec.intent_id, rec.coid)
            return dict(env, raw=venue_error), code
        env = self._spine(v, None, "rejected", {"code": res.code, "message": res.message}, rec.intent_id, rec.coid)
        return dict(env, raw=venue_error), 1

    def _settle(self, rec: IntentRecord, v: Venue, res: SubmitResult) -> tuple[dict, int]:
        env, code = self._map_submit(rec, res)
        if res.kind == "new":
            self._count_daily(rec.account, res.order)
        self._finish(rec, res, env, code)
        return env, code

    def _finish(self, rec: IntentRecord, res: SubmitResult, env: dict, code: int) -> None:
        if code == 2:
            rec.state, rec.outcome = "unknown", (env, code)
            return
        rec.state, rec.outcome = "done", (env, code)
        if res.order is not None and res.kind in ("new", "duplicate"):
            rec.order_id = res.order.id
            self.order_source.setdefault(res.order.id, "broker")

    def order_cancel(self, flags: dict) -> tuple[dict, int]:
        acct = self._account(flags)
        v = self.venues[acct.venue]
        o, err, code = self._resolve_order(flags, acct)
        if err is not None:
            if code == 1:
                err["status"] = "rejected"
            return err, code
        intent = str(flags["intent-id"]) if flags.get("intent-id") else None
        res = v.cancel(o.id, self.clock.now_ms())
        if res.kind == "rejected":
            # Already terminal: report the order as it stands (a filled order
            # is not "never placed"), with the reason the cancel did nothing.
            env = self._spine(v, res.order, intent=intent, reason={"code": res.code, "message": res.message})
            return env, 1 if env["status"] == "rejected" else 0
        return self._spine(v, res.order, intent=intent), 0

    # ================================================================ trading REST surface

    def portfolio(self, account_id: str) -> dict:
        a = self.accounts[account_id]
        v = self.venues[a.venue]
        now = self.clock.now_ms()
        quotes = {m.quote for m in v.markets.values()}
        bal = v.balances.get(a.id, {})
        cash = sum(b["free"] + b["used"] for k, b in bal.items() if k in quotes)
        assets, upnl, equity = [], 0.0, cash
        for p in v.positions_view(a.id, now):
            mv = p["notional"] or 0.0
            equity += mv
            upnl += p["unrealizedPnl"] or 0.0
            assets.append({"symbol": self.canonical(a, p["symbol"]), "side": "long", "quantity": p["contracts"],
                           "entryPrice": p["entryPrice"], "currentPrice": p["markPrice"],
                           "marketValue": mv, "unrealizedPnl": p["unrealizedPnl"]})
        for x in assets:
            x["allocation"] = x["marketValue"] / equity if equity else 0.0
        return {"equity": round(equity, 2), "cash": round(cash, 2), "unrealizedPnl": round(upnl, 2), "assets": assets}

    def trading_orders(self, account_id: str, limit: int | None = None, source: str | None = None,
                       since_ms: int | None = None) -> list[dict]:
        a = self.accounts[account_id]
        v = self.venues[a.venue]
        out = []
        for o in v.orders.values():
            if o.account != a.id:
                continue
            if source and self.order_source.get(o.id, "broker") != source:
                continue
            if since_ms is not None and o.created_ms < since_ms:
                continue
            out.append({"orderId": o.id, "symbol": self.canonical(a, o.symbol), "side": o.side, "requestedQty": o.amount,
                        "filledQty": o.filled, "price": o.average or o.limit_price or 0.0,
                        "status": {"closed": "filled"}.get(o.status, o.status), "rejectReason": o.reject_reason,
                        "source": self.order_source.get(o.id, "broker"), "sourcePlaybook": "", "dryRun": False,
                        "createdAtMs": o.created_ms})
        return out[-limit:] if limit else out

    def equity_history(self, account_id: str, timeframe: str = "1d", since_ms: int | None = None,
                       until_ms: int | None = None) -> list[dict]:
        """Equity at each timeframe step, rebuilt from the starting balances,
        the fill ledger and the price at that time."""
        step = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000,
                "1d": 86_400_000, "1w": 604_800_000}.get(timeframe)
        if step is None:
            raise ValueError(f"timeframe must be one of 1m, 5m, 15m, 1h, 4h, 1d, 1w, got {timeframe!r}")
        a = self.accounts[account_id]
        v = self.venues[a.venue]
        now = self.clock.now_ms()
        until = min(until_ms if until_ms is not None else now, now)
        since = since_ms if since_ms is not None else until - 30 * step
        since = max(since, until - 1000 * step)
        if since > until:
            return []
        fills = [f for f in self.rec.fills if f["account"] == a.id and f["venue"] == v.name]
        quotes = {m.quote for m in v.markets.values()}
        points, base = [], None
        t = until - (until - since) // step * step
        while t <= until:
            hold = dict(v.initial.get(a.id, {}))
            for f in fills:
                if f["time"] > t:
                    continue
                m = v.markets[f["symbol"]]
                sign = 1 if f["side"] == "buy" else -1
                hold[m.base] = hold.get(m.base, 0.0) + sign * f["qty"]
                hold[m.quote] = hold.get(m.quote, 0.0) - sign * f["qty"] * f["price"] - f["fee"]
            equity = sum(q for asset, q in hold.items() if asset in quotes)
            for m in v.markets.values():
                if hold.get(m.base) and m.quote in quotes and m.base not in quotes:
                    equity += hold[m.base] * (self.md.price(m.instrument, t) or 0.0)
                    hold[m.base] = 0.0  # count each asset once even if it trades on several markets
            base = equity if base is None else base
            pnl = equity - base
            points.append({"timestamp": t, "equity": round(equity, 2), "pnl": round(pnl, 2),
                           "pnlPct": round(pnl / base * 100, 4) if base else 0.0})
            t += step
        return points

    def execute_signal(self, account_id: str, signal_json: str, dry_run: bool) -> tuple[dict, int]:
        """`alva trading execute`: allocate-weight signals, no idempotency key."""
        a = self.accounts.get(account_id)
        if a is None:
            return {"status": "error", "orders": [], "error": f"account {account_id} not found"}, 404
        v = self.venues[a.venue]
        try:
            signals = json.loads(signal_json)
            if isinstance(signals, dict):
                signals = [signals]
        except json.JSONDecodeError:
            return {"status": "error", "orders": [], "error": "signal must be JSON"}, 400
        if not isinstance(signals, list) or not all(isinstance(x, dict) for x in signals):
            return {"status": "error", "orders": [], "error": "signal must be a list of signal objects"}, 400
        now = self.clock.now_ms()
        port = self.portfolio(a.id)
        orders = []
        for s in signals:
            ins = s.get("instruction") or {}
            if not isinstance(ins, dict) or ins.get("type") != "allocate":
                return {"status": "error", "orders": [], "error": "only allocate instructions are supported"}, 400
            for w in ins.get("weights") or []:
                canonical = str(w.get("symbol", ""))
                problem = self._symbol_problem(a, canonical)
                if problem:
                    return {"status": "error", "orders": [], "error": problem}, 400
                sym = self._venue_symbol(v, canonical)
                if sym is None:
                    orders.append({"orderId": "", "symbol": canonical, "side": "", "requestedQty": 0,
                                   "filledQty": 0, "price": 0, "status": "rejected", "rejectReason": "unknown_symbol"})
                    continue
                px = v.mid(sym, now) or 0.0
                held = next((x["quantity"] for x in port["assets"] if x["symbol"] == canonical), 0.0)
                target = float(w.get("weight", 0)) * port["equity"] / px if px else 0.0
                delta = target - held
                if abs(delta) * px < 1.0:
                    continue
                side, qty = ("buy", delta) if delta > 0 else ("sell", -delta)
                qty = round(qty, v.markets[sym].amount_precision)
                # The same trex admission gate (risk rules, limits) as `alva broker`.
                ok, reason, _ = self._admit(a, v, {"venue": None, "symbol": sym, "amount": qty, "type": "market",
                                                   "price": None})
                if not ok:
                    orders.append({"orderId": "", "symbol": canonical, "side": side, "requestedQty": qty,
                                   "filledQty": 0, "price": round(px, 2), "status": "rejected",
                                   "rejectReason": reason["code"]})
                    continue
                if dry_run:
                    orders.append({"orderId": "", "symbol": canonical, "side": side, "requestedQty": qty, "filledQty": 0,
                                   "price": round(px, 2), "status": "simulated"})
                    continue
                res = v.submit({"account": a.id, "symbol": sym, "side": side, "amount": qty, "type": "market",
                                "price": None, "coid": None}, now)
                if res.kind == "new":
                    self.order_source[res.order.id] = "trading"
                    self._count_daily(a.id, res.order)
                    o = res.order
                    orders.append({"orderId": o.id, "symbol": canonical, "side": side, "requestedQty": qty,
                                   "filledQty": o.filled, "price": o.average or px,
                                   "status": {"closed": "filled"}.get(o.status, o.status)})
                else:
                    orders.append({"orderId": "", "symbol": canonical, "side": side, "requestedQty": qty, "filledQty": 0,
                                   "price": round(px, 2), "status": "rejected", "rejectReason": res.code})
        return {"status": "ok", "orders": orders}, 200

    # Trading REST symbols are canonical Arrays pairs that match the account's
    # exchange (trading.md): binance_spot -> BINANCE_SPOT_BTC_USDT, alpaca ->
    # US_SPOT_AAPL_USD. The simulator has spot markets only. Assumption: an
    # exchange without a suffix uses <EXCHANGE>_SPOT_ for its spot pairs, and
    # Alpaca crypto uses US_SPOT_ like its stocks (the docs show neither).

    def _symbol_prefix(self, a: Account) -> str:
        ex = a.exchange.lower()
        if ex == "alpaca":
            return "US_SPOT_"
        return ex.removesuffix("_spot").upper() + "_SPOT_"

    def canonical(self, a: Account, venue_symbol: str) -> str:
        m = self.venues[a.venue].markets[venue_symbol]
        return f"{self._symbol_prefix(a)}{m.base}_{m.quote}"

    def _symbol_problem(self, a: Account, canonical: str) -> str | None:
        parts = canonical.split("_")
        if len(parts) != 4 or not all(p.isalnum() and p.upper() == p for p in parts):
            return (f"symbol {canonical!r} is not a canonical trading pair such as "
                    f"{self._symbol_prefix(a)}BTC_USDT; resolve it with 'alva trading-pairs'")
        if not canonical.startswith(self._symbol_prefix(a)):
            return f"symbol {canonical} does not match account exchange {a.exchange}"
        return None

    def trading_pairs(self, q: str) -> list[dict]:
        """`alva trading-pairs search`: the canonical pairs of the simulated
        accounts' markets whose ticker or full pair matches q."""
        out, seen = [], set()
        for a in self.accounts.values():
            for sym, m in self.venues[a.venue].markets.items():
                pair = self.canonical(a, sym)
                if pair in seen or q.strip().upper() not in (m.base.upper(), pair):
                    continue
                seen.add(pair)
                out.append({"trading_pair": pair, "base": m.base, "market": pair.split("_")[0],
                            "instrument_type": "spot", "quote": m.quote,
                            "type": ("stock" if m.session == "us_equity" else "crypto") + "-spot"})
        return out

    def _venue_symbol(self, v: Venue, canonical: str) -> str | None:
        """'BINANCE_SPOT_BTC_USDT' -> 'BTC/USDT', 'US_SPOT_AAPL_USD' -> 'AAPL'."""
        parts = canonical.split("_")
        if len(parts) != 4 or parts[1] != "SPOT":
            return None
        base, quote = parts[2], parts[3]
        for cand in (f"{base}/{quote}", base, f"{base}-{quote}"):
            if cand in v.markets:
                return cand
        return None

    # ================================================================ state

    def state(self) -> dict:
        return {
            "now": self.clock.now_ms(),
            "calls": self.calls,
            "trex": asdict(self.trex),
            "intents": {k: {"intent": r.intent_id, "coid": r.coid, "state": r.state, "order": r.order_id,
                            "attempts": r.attempts, "account": r.account, "venue": r.venue}
                        for k, r in self.intents.items()},
            "venues": {n: v.state() for n, v in self.venues.items()},
            "pending": self.pending,
        }
