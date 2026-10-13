"""HTTP surface: the subset of Alva's REST API the `alva` CLI uses for trading.

``App.handle`` is a plain function from a request to a response, so the same
code serves real HTTP (server.py) and in-process replay (replay.py). Transport
faults are returned as instructions (drop the connection, sleep first) for the
HTTP layer to carry out.
"""

from __future__ import annotations

import json
import re
import traceback
from dataclasses import dataclass, field
from urllib.parse import parse_qs

from .alfs import FsError
from .util import iso

CONTROL_PREFIX = "/__sim/"


@dataclass
class Request:
    method: str
    path: str
    query: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)  # lower-case names
    body: bytes = b""

    @classmethod
    def from_parts(cls, method: str, raw_path: str, headers: dict, body: bytes) -> "Request":
        path, _, qs = raw_path.partition("?")
        query = {k: v[-1] for k, v in parse_qs(qs, keep_blank_values=True).items()}
        return cls(method.upper(), path, query, {k.lower(): v for k, v in headers.items()}, body)

    def json(self) -> dict:
        if not self.body:
            return {}
        try:
            v = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return v if isinstance(v, dict) else {}


@dataclass
class Response:
    status: int
    body: bytes = b""
    content_type: str = "application/json"
    headers: dict = field(default_factory=dict)
    action: str | None = None  # None (send) | "drop" (close without answering)
    delay_ms: int = 0  # wall-clock sleep before the action (hang faults)

    @classmethod
    def of_json(cls, status: int, payload, **kw) -> "Response":
        return cls(status, (json.dumps(payload) + "\n").encode(), "application/json", **kw)


def api_error(status: int, code: str, message: str, **kw) -> Response:
    return Response.of_json(status, {"error": {"code": code, "message": message}}, **kw)


TRADING_ROUTES = {"accounts", "risk-rules", "portfolio", "orders", "subscriptions", "equity-history", "execute"}
FS_ROUTES = {"read", "write", "stat", "readdir", "mkdir", "remove"}

HTTP_CODES = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND",
              429: "RATE_LIMITED", 500: "INTERNAL", 502: "UNAVAILABLE", 503: "UNAVAILABLE", 504: "DEADLINE_EXCEEDED"}


class App:
    def __init__(self, sim, control_token: str | None = None):
        self.sim = sim
        self.control_token = control_token
        self.unknown_routes: dict[str, int] = {}

    # ------------------------------------------------------------ entry

    def handle(self, req: Request) -> Response:
        try:
            return self._handle(req)
        except Exception as e:  # a simulator bug must not look like a lost reply
            self.sim.rec.event("sim", "error", "internal", {"path": req.path, "error": repr(e)[:500],
                                                             "trace": traceback.format_exc(limit=6)[-2000:]})
            return api_error(500, "INTERNAL", "internal error")

    def _handle(self, req: Request) -> Response:
        if req.path.startswith(CONTROL_PREFIX):
            return self._control(req)
        sim = self.sim
        sim.clock.tick()
        sim.broker.tick()
        sim.rec.request({"method": req.method, "path": req.path, "query": req.query,
                         "body": req.body.decode("utf-8", "replace"),
                         "auth": "ok" if req.headers.get("x-alva-api-key") == sim.api_key else "bad"})
        op = self._classify(req)
        f = op["f"]
        sim.rec.invoke(f, {k: v for k, v in op.items() if k not in ("f",)})

        if op["route"] == "other":
            # Before the key check: some of these (data skills) are called
            # without a key, and a 401 would send an agent to `alva auth login`.
            self.unknown_routes[op["f"]] = self.unknown_routes.get(op["f"], 0) + 1
            return self._finish_error(op, 404, "NOT_FOUND", f"{req.path} not found")
        if req.headers.get("x-alva-api-key") != sim.api_key:
            return self._finish_error(op, 401, "UNAUTHENTICATED", "Missing or invalid API key")

        fault = sim.nemesis.pick("gateway", op)
        kind = fault["kind"] if fault else None
        if kind == "http":
            status = int(fault.get("status", 500))
            headers = {}
            if fault.get("retry_after_s") is not None:
                headers["Retry-After"] = str(fault["retry_after_s"])
            return self._finish_error(op, status, fault.get("code", HTTP_CODES.get(status, "UNKNOWN")),
                                      fault.get("message", f"HTTP {status}"), headers=headers)
        if kind == "drop_before":
            self._complete(op, None, {"transport": "dropped", "applied": False})
            return Response(0, action="drop")
        if kind == "hang" and not fault.get("commit", True):
            self._complete(op, None, {"transport": "hang_then_" + fault.get("then", "drop"), "applied": False})
            if fault.get("then", "drop") == "drop":
                return Response(0, action="drop", delay_ms=int(fault.get("ms", 60_000)))
            return api_error(504, "DEADLINE_EXCEEDED", "upstream timeout", delay_ms=int(fault.get("ms", 60_000)))

        resp, exit_code, summary = self._process(req, op, fault)

        if kind == "drop_after":
            self._complete(op, None, {"transport": "dropped", "applied": True, "server_saw": summary})
            return Response(0, action="drop")
        if kind == "hang":
            then = fault.get("then", "drop")
            delay = int(fault.get("ms", 60_000))
            if then == "drop":
                self._complete(op, None, {"transport": "hang_then_drop", "applied": True, "server_saw": summary})
                resp.action, resp.delay_ms = "drop", delay
                return resp
            resp.delay_ms = delay
        if kind == "delay":
            resp.delay_ms = int(fault.get("ms", 1000))
        self._complete(op, exit_code, summary)
        return resp

    # ------------------------------------------------------------ bookkeeping

    def _complete(self, op: dict, exit_code: int | None, value: dict) -> None:
        """Record what the client observes. exit None = transport failure."""
        v = dict(value)
        if op["route"] == "broker" and exit_code is None:
            v["cli_exit"] = 2  # the CLI turns any transport failure into exit 2
        self.sim.rec.complete(op["f"], exit_code if exit_code is not None else 2, v)

    def _finish_error(self, op: dict, status: int, code: str, message: str, headers: dict | None = None) -> Response:
        # The history records what the client can conclude. For `alva broker`
        # every HTTP error becomes exit 2 (handleBroker catches it), so the
        # client cannot tell a 401 from a lost reply; other commands exit 1.
        if op["route"] == "broker":
            self.sim.rec.complete(op["f"], 2, {"http": status, "code": code, "applied": False, "cli_exit": 2})
        else:
            semantic = 2 if status >= 500 or status == 429 else 1
            self.sim.rec.complete(op["f"], semantic, {"http": status, "code": code, "applied": False, "cli_exit": 1})
        return api_error(status, code, message, headers=headers or {})

    def _classify(self, req: Request) -> dict:
        p = req.path
        if p == "/api/v1/broker/invoke" and req.method == "POST":
            body = req.json()
            argv = [str(a) for a in body.get("argv", [])] if isinstance(body.get("argv"), list) else []
            op = self.sim.broker.peek_op(argv, body.get("stdin"))
            op["f"] = "broker " + (op.get("cmd") or "?")
            op["argv"] = argv
            return op
        m = re.match(r"^/api/v1/trading/([a-z-]+)$", p)
        if m and m.group(1) in TRADING_ROUTES:
            cmd = m.group(1)
            body = req.json()
            account = req.query.get("accountId") or body.get("accountId")
            live = cmd == "execute" and not body.get("dryRun", False)
            return {"route": "trading", "cmd": cmd, "f": f"trading {cmd}", "account": account, "live": live}
        m = re.match(r"^/api/v1/fs/([a-z]+)$", p)
        if m and m.group(1) in FS_ROUTES:
            path = req.query.get("path") or req.json().get("path") or ""
            return {"route": "fs", "cmd": m.group(1), "f": f"fs {m.group(1)}", "path": self.sim.alfs.norm(path) if path else ""}
        if p == "/api/v1/me":
            return {"route": "me", "cmd": "me", "f": "me"}
        if p == "/api/v1/trading-pairs/search":
            return {"route": "trading-pairs", "cmd": "search", "f": "trading-pairs search"}
        if p in ("/api/v1/arrays-jwt/status", "/api/v1/arrays-jwt/ensure"):
            return {"route": "arrays-jwt", "cmd": p.rsplit("/", 1)[-1], "f": "arrays-jwt"}
        return {"route": "other", "cmd": p, "f": f"{req.method} {p}"}

    # ------------------------------------------------------------ routes

    def _process(self, req: Request, op: dict, fault: dict | None) -> tuple[Response, int, dict]:
        route = op["route"]
        kind = fault["kind"] if fault else None
        if route == "broker":
            body = req.json()
            lag = int(fault.get("lag_ms", 0)) if kind == "stale" else 0
            env, code = self.sim.broker.invoke(op["argv"], body.get("stdin"), data_lag_ms=lag)
            summary = {"exit": code}
            if isinstance(env, dict):
                summary.update({k: env.get(k) for k in ("status", "reason", "orderId", "intentId", "filled") if k in env})
            return Response.of_json(200, {"envelope": env, "exit": code}), code, summary
        try:
            if route == "trading":
                return self._trading(req, op)
            if route == "fs":
                return self._fs(req, op, corrupt=(kind == "corrupt"))
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            return api_error(400, "INVALID_ARGUMENT", f"bad request: {e}"), 1, {"http": 400}
        if route == "me":
            u = self.sim.scenario["user"]
            return Response.of_json(200, {
                "id": u["id"], "username": u["username"], "subscription_tier": u.get("tier", "pro"),
                "telegram_username": "", "discord_username": "", "slack_username": "",
                "whatsapp_username": "", "imessage_username": "", "active_channel": "",
                "toolkit_min_version": "0.31.0", "home_path": self.sim.alfs.home,
            }), 0, {}
        if route == "trading-pairs":
            pairs = self.sim.broker.trading_pairs(str(req.query.get("q", "")))
            return Response.of_json(200, {"trading_pairs": pairs}), 0, {"pairs": len(pairs)}
        if route == "arrays-jwt":  # toolkit types: ArraysJwtStatusResponse / EnsureArraysJwtResponse
            exp_s = (self.sim.clock.now_ms() + 30 * 86_400_000) // 1000
            tier = {"free": "SUBSCRIPTION_TIER_FREE"}.get(self.sim.scenario["user"].get("tier", "pro"),
                                                          "SUBSCRIPTION_TIER_PRO")
            if op["cmd"] == "ensure":
                return Response.of_json(200, {"expires_at": exp_s, "tier": tier, "renewed": False}), 0, {}
            return Response.of_json(200, {"exists": True, "expires_at": exp_s, "tier": tier,
                                          "renewal_needed": False}), 0, {}
        return api_error(404, "NOT_FOUND", f"{req.path} not found"), 1, {"http": 404}

    def _trading(self, req: Request, op: dict) -> tuple[Response, int, dict]:
        b = self.sim.broker
        cmd = op["cmd"]
        acct = op.get("account")
        if cmd == "accounts" and req.method == "GET":
            return Response.of_json(200, [b.account_view(a, rest=True) for a in b.accounts.values()]), 0, {}
        if cmd == "risk-rules":
            if req.method == "PUT":
                body = req.json()
                for k in ("maxSingleOrder", "maxDailyTurnover", "maxDailyOrders"):
                    if k in body:
                        b.risk_rules[k] = {"value": float(body[k]["value"]), "enabled": bool(body[k]["enabled"])}
            return Response.of_json(200, b.risk_rules_view()), 0, {}
        if acct is None or acct not in b.accounts:
            return api_error(404, "NOT_FOUND", f"account {acct} not found"), 1, {"http": 404}
        if cmd == "portfolio":
            return Response.of_json(200, b.portfolio(acct)), 0, {}
        if cmd == "orders":
            q = req.query
            limit = int(q["limit"]) if q.get("limit") else None
            since = int(q["since"]) if q.get("since") else None
            return Response.of_json(200, b.trading_orders(acct, limit, q.get("source") or None, since)), 0, {}
        if cmd == "subscriptions":
            return Response.of_json(200, []), 0, {}
        if cmd == "equity-history":
            q = req.query
            points = b.equity_history(acct, q.get("timeframe") or "1d",
                                      int(q["sinceMs"]) if q.get("sinceMs") else None,
                                      int(q["untilMs"]) if q.get("untilMs") else None)
            return Response.of_json(200, points), 0, {"points": len(points)}
        if cmd == "execute" and req.method == "POST":
            body = req.json()
            result, status = b.execute_signal(acct, str(body.get("signalJson", "")), bool(body.get("dryRun", False)))
            if status != 200:
                return api_error(status, HTTP_CODES.get(status, "INVALID_ARGUMENT"), result.get("error", "")), 1, {"http": status}
            return Response.of_json(200, result), 0, {"orders": len(result["orders"])}
        return api_error(404, "NOT_FOUND", f"{req.path} not found"), 1, {"http": 404}

    def _fs(self, req: Request, op: dict, corrupt: bool = False) -> tuple[Response, int, dict]:
        fs = self.sim.alfs
        cmd = op["cmd"]
        try:
            if cmd == "read":
                off = int(req.query.get("offset") or 0)
                size = int(req.query["size"]) if req.query.get("size") else None
                data = fs.read(req.query.get("path", ""), off, size)
                if corrupt:
                    data = bytes((c * 7 + 13) % 256 for c in data[: max(8, len(data) // 2)])
                return Response(200, data, "application/octet-stream"), 0, {"bytes": len(data)}
            if cmd == "write" and req.method == "POST":
                if req.headers.get("content-type", "").startswith("application/json"):
                    body = req.json()
                    n = fs.write(str(body.get("path", "")), str(body.get("data", "")).encode(),
                                 append=bool(body.get("flags")), mkdir_parents=body.get("mkdir_parents", True) is not False)
                else:
                    n = fs.write(req.query.get("path", ""), req.body, append=bool(req.query.get("flags")),
                                 mkdir_parents=req.query.get("mkdir_parents", "true") != "false")
                return Response.of_json(200, {"bytes_written": n}), 0, {"bytes": n}
            if cmd == "stat":
                return Response.of_json(200, fs.stat(req.query.get("path", ""))), 0, {}
            if cmd == "readdir":
                return Response.of_json(200, fs.readdir(req.query.get("path", ""),
                                                        req.query.get("recursive") == "true")), 0, {}
            if cmd == "mkdir" and req.method == "POST":
                fs.mkdir(str(req.json().get("path", "")))
                return Response(204), 0, {}
            if cmd == "remove" and req.method == "DELETE":
                fs.remove(req.query.get("path") or str(req.json().get("path", "")),
                          (req.query.get("recursive") or "") == "true" or bool(req.json().get("recursive")))
                return Response(204), 0, {}
        except FsError as e:
            return api_error(e.status, e.code, e.message), 1, {"http": e.status}
        return api_error(404, "NOT_FOUND", f"{req.path} not found"), 1, {"http": 404}

    # ------------------------------------------------------------ control plane

    def _control(self, req: Request) -> Response:
        if not self.control_token or req.headers.get("x-sim-control") != self.control_token:
            return api_error(404, "NOT_FOUND", f"{req.path} not found")
        what = req.path[len(CONTROL_PREFIX):]
        sim = self.sim
        if what == "state":
            return Response.of_json(200, dict(sim.state(), unknown_routes=self.unknown_routes))
        if what == "history":
            return Response.of_json(200, {"events": sim.rec.events, "fills": sim.rec.fills})
        if what == "advance" and req.method == "POST":
            ms = int(req.json().get("ms", 0))
            sim.clock.advance(ms)
            sim.broker.tick()
            return Response.of_json(200, {"now": sim.clock.now_ms(), "iso": iso(sim.clock.now_ms())})
        if what == "settle" and req.method == "POST":
            return Response.of_json(200, {"committed": sim.broker.settle()})
        if what == "flush" and req.method == "POST":
            sim.rec.write_state(dict(sim.state(), unknown_routes=self.unknown_routes))
            return Response.of_json(200, {"ok": True})
        return api_error(404, "NOT_FOUND", f"{req.path} not found")
