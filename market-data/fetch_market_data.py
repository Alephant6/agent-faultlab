#!/usr/bin/env python3
"""Download the historical market data the fake Alva server replays.

Stdlib only, so it runs on a bare server (python3 >= 3.8). Nothing here is
committed data: each user downloads it themselves under the source's terms.

Sources
  binance  data.binance.vision bulk files, no key. CC BY-NC-SA 4.0 plus the
           Binance Vision Dataset Terms (non-commercial research use only).
  alpaca   Alpaca Market Data API v2 (US stocks). Needs a free Alpaca key in
           APCA_API_KEY_ID / APCA_API_SECRET_KEY. Never put keys in the repo.

Output (one CSV per symbol and interval, all times UTC):
  OUT/SOURCE/SYMBOL/INTERVAL.csv
      ts_ms,ts_utc,open,high,low,close,volume,trades,vwap
  OUT/binance/SYMBOL/funding_8h.csv   (USD-M perpetual funding)
      ts_ms,ts_utc,funding_rate,interval_hours
  OUT/manifest.json  source URL, sha256 and row count of every file

Usage
  python3 -I fetch_market_data.py --out data            # defaults below
  python3 -I fetch_market_data.py --only binance        # no key needed
"""

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# Minimum scope (see README.md for why). Change with flags, not by editing.
PIN_DATE = "2025-06-13"            # the fake server's "today"
REPLAY = ("2025-06-09", "2025-06-20")   # 1-minute bars: two trading weeks
LOOKBACK = ("2024-06-01", "2025-06-30")  # daily bars: ~1 year for indicators
CRYPTO = ["BTCUSDT", "ETHUSDT"]
STOCKS = ["AAPL", "NVDA", "SPY"]

BINANCE = "https://data.binance.vision/data"
ALPACA = "https://data.alpaca.markets/v2/stocks/bars"
UA = {"User-Agent": "alva-sim-fetch/0.1 (research; non-commercial)"}

BAR_COLS = ["ts_ms", "ts_utc", "open", "high", "low", "close", "volume",
            "trades", "vwap"]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def get(url, headers=None, tries=4):
    """GET with polite backoff on 429/5xx. Returns bytes, or None on 404."""
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(2 ** (i + 1))
                continue
            raise
        except urllib.error.URLError:
            if i < tries - 1:
                time.sleep(2 ** (i + 1))
                continue
            raise
    return None


def iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def to_ms(raw):
    """Binance spot files switched from ms to us timestamps on 2025-01-01."""
    t = int(raw)
    return t // 1000 if t > 10 ** 14 else t


def days(start, end):
    d = dt.date.fromisoformat(start)
    stop = dt.date.fromisoformat(end)
    while d <= stop:
        yield d
        d += dt.timedelta(days=1)


def months(start, end):
    seen = []
    for d in days(start, end):
        m = d.strftime("%Y-%m")
        if m not in seen:
            seen.append(m)
    return seen


def binance_zip(path):
    """Fetch one bulk zip, verify it against its .CHECKSUM, return CSV text."""
    url = f"{BINANCE}/{path}"
    blob = get(url)
    if blob is None:
        return url, None
    check = get(url + ".CHECKSUM")
    if check:
        want = check.decode().split()[0]
        got = hashlib.sha256(blob).hexdigest()
        if want != got:
            raise RuntimeError(f"checksum mismatch for {url}")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return url, z.read(z.namelist()[0]).decode()


def binance_klines(sym, interval, start, end):
    """Daily files for 1m bars, monthly files for 1d bars."""
    if interval == "1d":
        paths = [f"spot/monthly/klines/{sym}/1d/{sym}-1d-{m}.zip"
                 for m in months(start, end)]
    else:
        paths = [f"spot/daily/klines/{sym}/{interval}/{sym}-{interval}-{d}.zip"
                 for d in days(start, end)]
    lo = int(dt.datetime.fromisoformat(start + "T00:00:00+00:00").timestamp() * 1000)
    hi = int(dt.datetime.fromisoformat(end + "T23:59:59+00:00").timestamp() * 1000)
    rows, urls = [], []
    for p in paths:
        url, text = binance_zip(p)
        if text is None:
            log(f"  missing {url}")
            continue
        urls.append(url)
        for r in csv.reader(io.StringIO(text)):
            if not r or not r[0].isdigit():
                continue  # some files carry a header row
            ms = to_ms(r[0])
            if not lo <= ms <= hi:
                continue
            vol, quote_vol = float(r[5]), float(r[7])
            vwap = quote_vol / vol if vol else float(r[4])
            rows.append([ms, iso(ms), r[1], r[2], r[3], r[4], r[5], r[8],
                         f"{vwap:.8f}"])
    rows.sort(key=lambda x: x[0])
    return rows, urls


def binance_funding(sym, start, end):
    rows, urls = [], []
    for m in months(start, end):
        url, text = binance_zip(
            f"futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{m}.zip")
        if text is None:
            log(f"  missing {url}")
            continue
        urls.append(url)
        for r in csv.reader(io.StringIO(text)):
            if not r or not r[0].isdigit():
                continue
            ms = int(r[0])
            rows.append([ms, iso(ms), r[2], r[1]])
    rows.sort(key=lambda x: x[0])
    return rows, urls


def alpaca_bars(symbols, timeframe, start, end, feed):
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("set APCA_API_KEY_ID and APCA_API_SECRET_KEY "
                           "(free Alpaca account; keep them out of the repo)")
    hdr = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    out = {s: [] for s in symbols}
    params = {"symbols": ",".join(symbols), "timeframe": timeframe,
              "start": start + "T00:00:00Z", "end": end + "T23:59:59Z",
              "adjustment": "split", "feed": feed, "limit": "10000",
              "sort": "asc"}
    first_url = None
    while True:
        url = ALPACA + "?" + urllib.parse.urlencode(params)
        first_url = first_url or url
        body = json.loads(get(url, hdr))
        for s, bars in (body.get("bars") or {}).items():
            for b in bars:
                ms = int(dt.datetime.fromisoformat(
                    b["t"].replace("Z", "+00:00")).timestamp() * 1000)
                out[s].append([ms, iso(ms), b["o"], b["h"], b["l"], b["c"],
                               b["v"], b.get("n", ""), b.get("vw", "")])
        token = body.get("next_page_token")
        if not token:
            break
        params["page_token"] = token
        time.sleep(0.4)  # Basic plan: 200 requests/min
    return out, first_url


def write(path, cols, rows, urls, manifest, out):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    rel = os.path.relpath(path, out)
    manifest["files"][rel] = {"rows": len(rows), "sha256": digest,
                              "sources": urls}
    log(f"  wrote {rel}: {len(rows)} rows")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out", default="data")
    ap.add_argument("--only", choices=["binance", "alpaca"])
    ap.add_argument("--crypto", default=",".join(CRYPTO))
    ap.add_argument("--stocks", default=",".join(STOCKS))
    ap.add_argument("--replay", nargs=2, default=REPLAY, metavar=("START", "END"))
    ap.add_argument("--lookback", nargs=2, default=LOOKBACK,
                    metavar=("START", "END"))
    ap.add_argument("--alpaca-feed", default="sip", choices=["sip", "iex"])
    a = ap.parse_args()

    manifest = {"pin_date": PIN_DATE, "replay": list(a.replay),
                "lookback": list(a.lookback),
                "generated_utc": dt.datetime.now(dt.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
                "licenses": {
                    "binance": "CC BY-NC-SA 4.0 + Binance Vision Dataset Terms "
                               "v1.0 (2026-08-26); attribution: Binance Vision",
                    "alpaca": "Alpaca market data terms; personal use, "
                              "do not redistribute"},
                "files": {}}
    # Keep entries from an earlier run (e.g. --only binance, then --only alpaca).
    try:
        with open(os.path.join(a.out, "manifest.json")) as f:
            manifest["files"] = json.load(f).get("files", {})
    except (OSError, ValueError):
        pass

    if a.only in (None, "binance"):
        for sym in filter(None, a.crypto.split(",")):
            log(f"binance {sym}")
            base = os.path.join(a.out, "binance", sym)
            rows, urls = binance_klines(sym, "1m", *a.replay)
            write(os.path.join(base, "1m.csv"), BAR_COLS, rows, urls, manifest, a.out)
            rows, urls = binance_klines(sym, "1d", *a.lookback)
            write(os.path.join(base, "1d.csv"), BAR_COLS, rows, urls, manifest, a.out)
            rows, urls = binance_funding(sym, *a.replay)
            write(os.path.join(base, "funding_8h.csv"),
                  ["ts_ms", "ts_utc", "funding_rate", "interval_hours"],
                  rows, urls, manifest, a.out)

    if a.only in (None, "alpaca"):
        syms = [s for s in a.stocks.split(",") if s]
        for tf, name, (start, end) in (("1Min", "1m", a.replay),
                                       ("1Day", "1d", a.lookback)):
            log(f"alpaca {tf} {','.join(syms)}")
            try:
                try:
                    bars, url = alpaca_bars(syms, tf, start, end, a.alpaca_feed)
                except urllib.error.HTTPError as e:
                    # Accounts without a SIP subscription get 403; IEX is free.
                    if e.code != 403 or a.alpaca_feed != "sip":
                        raise
                    log("  sip refused (403), retrying with --alpaca-feed iex")
                    a.alpaca_feed = "iex"
                    bars, url = alpaca_bars(syms, tf, start, end, a.alpaca_feed)
            except (RuntimeError, urllib.error.HTTPError) as e:
                log(f"  skipped alpaca: {e}")
                break
            # Strip the query string so the manifest never echoes request params.
            for s in syms:
                write(os.path.join(a.out, "alpaca", s, f"{name}.csv"), BAR_COLS,
                      bars[s], [url.split("?")[0]], manifest, a.out)

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    log(f"done: {len(manifest['files'])} files in {a.out}")


if __name__ == "__main__":
    main()
