"""brokersim command line.

    python -m brokersim fetch-data                 # download pinned price files
    python -m brokersim serve --scenario scenarios/v2-drop-after-fill.json --out runs/v2
    python -m brokersim check runs/v2 --scenario scenarios/v2-drop-after-fill.json
    python -m brokersim replay runs/v2 --scenario scenarios/v2-drop-after-fill.json
    python -m brokersim explore                    # compliant client x venue presets x faults
    python -m brokersim selftest
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import unittest

from . import checker, explore, marketdata, replay, server
from .history import load_jsonl
from .sim import PACKAGE_ROOT, build, load_market_data, load_scenario, resolve
from .util import parse_iso_ms


def _market(sc: dict):
    try:
        return load_market_data(sc["data"]), "historical"
    except FileNotFoundError:
        day = parse_iso_ms(sc["clock"]["start"])
        day -= day % 86_400_000
        return marketdata.synthetic_market(day), "synthetic"


def cmd_fetch(a) -> int:
    n = marketdata.fetch_manifest(resolve(a.manifest), resolve(a.cache))
    print(f"fetched {n} file(s); cache verified at {resolve(a.cache)}")
    return 0


RUN_FILES = ("scenario.json", "history.jsonl", "ledger.jsonl", "requests.jsonl", "state.json")


def _archive_previous_run(out: str) -> None:
    """Move an earlier run's files into OUT/archive/<time>/ so a restart (for
    example by systemd) never overwrites a recorded history."""
    present = [n for n in RUN_FILES if os.path.exists(os.path.join(out, n))]
    if not present:
        return
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(os.path.getmtime(os.path.join(out, present[0]))))
    dest = os.path.join(out, "archive", stamp)
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(out, "archive", f"{stamp}-{n}")
    os.makedirs(dest)
    for name in present:
        os.replace(os.path.join(out, name), os.path.join(dest, name))
    sys.stderr.write(f"previous run moved to {dest}\n")


def cmd_serve(a) -> int:
    sc = load_scenario(a.scenario) if a.scenario else load_scenario({})
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        _archive_previous_run(a.out)
        with open(os.path.join(a.out, "scenario.json"), "w", encoding="utf-8") as f:
            json.dump(sc, f, indent=2)
    md = None
    if a.allow_synthetic:
        md, kind = _market(sc)
        sys.stderr.write(f"market data: {kind}\n")
    sim = build(sc, out_dir=a.out, md=md)
    token = a.control_token or secrets.token_hex(8)
    sys.stderr.write(f"api key: {sim.api_key}\ncontrol token: {token} (header X-Sim-Control on /__sim/*)\n")
    server.serve(sim, a.host, a.port, token, a.verbose)
    return 0


def cmd_check(a) -> int:
    expect = None
    if a.scenario:
        expect = load_scenario(a.scenario).get("expect")
    elif os.path.exists(os.path.join(a.run_dir, "scenario.json")):
        expect = load_scenario(os.path.join(a.run_dir, "scenario.json")).get("expect")
    r = checker.check_run_dir(a.run_dir, expect)
    print(json.dumps(r, indent=2))
    return 0 if r["valid"] else 1


def cmd_replay(a) -> int:
    sc_path = a.scenario or os.path.join(a.run_dir, "scenario.json")
    sc = load_scenario(sc_path)
    md, _ = _market(sc) if a.allow_synthetic else (None, "")
    events, fills = replay.replay(sc, os.path.join(a.run_dir, "requests.jsonl"), md=md)
    recorded = load_jsonl(os.path.join(a.run_dir, "history.jsonl"))
    diffs = replay.diff_histories(recorded, events)
    if diffs:
        print("\n".join(diffs))
        return 1
    print(f"replayed {len(events)} events and {len(fills)} fills: identical to the recording")
    return 0


def cmd_explore(a) -> int:
    sc = load_scenario({})
    md, kind = _market(sc)
    results = explore.explore(md, a.presets.split(",") if a.presets else None,
                              a.variants.split(",") if a.variants else None)
    print(f"market data: {kind}\n")
    print(explore.markdown_table(results))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
    return 0


def cmd_selftest(a) -> int:
    ok = True
    for name, passed in checker.selftest():
        print(("PASS " if passed else "FAIL ") + "checker: " + name)
        ok &= passed
    suite = unittest.defaultTestLoader.discover(os.path.join(PACKAGE_ROOT, "tests"), top_level_dir=PACKAGE_ROOT)
    res = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if ok and res.wasSuccessful() else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="brokersim", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch-data", help="download and verify the pinned historical price files")
    f.add_argument("--manifest", default="data/datasets.json")
    f.add_argument("--cache", default="data/cache")
    f.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("serve", help="serve the Alva API surface over HTTP")
    s.add_argument("--scenario")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--out", help="directory for history.jsonl, ledger.jsonl, requests.jsonl, state.json")
    s.add_argument("--control-token")
    s.add_argument("--allow-synthetic", action="store_true", help="use made-up prices if the cache is missing")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(fn=cmd_serve)

    c = sub.add_parser("check", help="check a recorded run")
    c.add_argument("run_dir")
    c.add_argument("--scenario")
    c.set_defaults(fn=cmd_check)

    r = sub.add_parser("replay", help="replay a recorded run and compare histories")
    r.add_argument("run_dir")
    r.add_argument("--scenario")
    r.add_argument("--allow-synthetic", action="store_true")
    r.set_defaults(fn=cmd_replay)

    e = sub.add_parser("explore", help="compliant client x venue presets x fault variants")
    e.add_argument("--presets")
    e.add_argument("--variants")
    e.add_argument("--json")
    e.set_defaults(fn=cmd_explore)

    t = sub.add_parser("selftest", help="checker self-test and unit tests")
    t.set_defaults(fn=cmd_selftest)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
