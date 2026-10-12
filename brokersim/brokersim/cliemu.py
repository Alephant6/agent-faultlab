"""In-process stand-in for the `alva broker` CLI path (toolkit-ts v0.31.0).

Mirrors ``handleBroker`` in src/cli/dispatch.ts: a live ``order place``
without ``--intent-id``/``--client-order-id`` gets a UUID minted before the
request and printed to stderr; any transport failure becomes a synthesized
``{status: error, reason: {code: network}}`` envelope with exit 2. Used by the
explorer and the tests so they exercise the same contract the real CLI sees,
without spawning Node.
"""

from __future__ import annotations

import json
import uuid

from .app import App, Request


class LocalCli:
    def __init__(self, app: App, api_key: str, seed: str = "cli"):
        self.app = app
        self.api_key = api_key
        self._seed = seed
        self._n = 0
        self.stderr: list[str] = []

    def mint(self) -> str:
        self._n += 1
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"brokersim:{self._seed}:{self._n}"))

    def broker(self, argv: list[str], stdin: dict | None = None) -> tuple[dict, int]:
        argv = list(argv)
        has = lambda name: any(a == name or a.startswith(name + "=") for a in argv)  # noqa: E731
        is_place = argv[:2] == ["order", "place"]
        stdin_text = json.dumps(stdin) if stdin is not None else None
        needs_id = stdin is None or (
            "intentId" not in stdin and "clientOrderId" not in stdin and stdin.get("dryRun", False) is False
        )
        if is_place and not has("--intent-id") and not has("--client-order-id") and not has("--dry-run") and needs_id:
            minted = self.mint()
            self.stderr.append(f"alva broker: intent-id {minted}")
            argv += ["--intent-id", minted]
        body = {"argv": argv}
        if stdin_text is not None:
            body["stdin"] = stdin_text
        req = Request("POST", "/api/v1/broker/invoke", {},
                      {"x-alva-api-key": self.api_key, "content-type": "application/json"},
                      json.dumps(body).encode())
        resp = self.app.handle(req)
        if resp.action == "drop" or resp.status != 200:
            msg = "fetch failed; cause: other side closed" if resp.action == "drop" else f"HTTP {resp.status}"
            return {"schemaVersion": 2, "status": "error", "reason": {"code": "network", "message": msg}}, 2
        payload = json.loads(resp.body)
        return payload["envelope"], payload.get("exit", 2)

    def rest(self, method: str, path: str, query: dict | None = None, body: dict | None = None):
        req = Request(method, path, {k: str(v) for k, v in (query or {}).items()},
                      {"x-alva-api-key": self.api_key, "content-type": "application/json"},
                      json.dumps(body).encode() if body is not None else b"")
        resp = self.app.handle(req)
        if resp.action == "drop":
            return None, None
        try:
            return resp.status, json.loads(resp.body) if resp.body else None
        except json.JSONDecodeError:
            return resp.status, resp.body
