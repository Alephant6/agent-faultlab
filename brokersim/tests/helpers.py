from brokersim.app import App
from brokersim.cliemu import LocalCli
from brokersim.marketdata import synthetic_market
from brokersim.sim import build
from brokersim.util import parse_iso_ms

DAY0 = parse_iso_ms("2025-06-13T00:00:00Z")
MD = synthetic_market(DAY0, days=2)
ACCT = "4471902"


def make(scenario=None):
    sim = build(scenario or {}, md=MD)
    app = App(sim, control_token="t")
    return sim, app, LocalCli(app, sim.api_key, seed="test")


def place(acct=ACCT, symbol="BTC/USDT", side="buy", amount="0.01", intent=None, extra=()):
    argv = ["order", "place", "--account", acct, "--venue", "binance", "--symbol", symbol,
            "--side", side, "--amount", amount]
    if intent:
        argv += ["--intent-id", intent]
    return argv + list(extra)
