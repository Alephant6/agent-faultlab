"""Historical price replay.

Prices come from public historical files (by default Binance's public kline
dumps at data.binance.vision, pinned by SHA-256). The simulator never serves a
bar that closes after the simulated "now", so an agent cannot see the future
even though the files contain it.
"""

from __future__ import annotations

import bisect
import csv
import datetime as _dt
import hashlib
import io
import json
import os
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Iterable

from .util import DAY_MS

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": DAY_MS,
}

# After the last bar closes, keep quoting its close for this long (weekends,
# holidays); past that the series counts as ended and quotes fail.
MAX_GAP_MS = 4 * DAY_MS


@dataclass(frozen=True)
class Bar:
    open_ms: int
    close_ms: int  # inclusive end of the bar
    o: float
    h: float
    l: float
    c: float
    v: float

    def as_ohlcv(self) -> list:
        return [self.open_ms, self.o, self.h, self.l, self.c, self.v]


class Series:
    """Bars of one instrument at one interval, sorted by open time."""

    def __init__(self, name: str, interval: str, bars: Iterable[Bar]):
        self.name = name
        self.interval = interval
        uniq = {b.open_ms: b for b in bars}
        self.bars = [uniq[k] for k in sorted(uniq)]
        self._opens = [b.open_ms for b in self.bars]

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def first_ms(self) -> int:
        return self.bars[0].open_ms

    @property
    def last_ms(self) -> int:
        return self.bars[-1].close_ms

    def bar_index_at(self, t_ms: int) -> int:
        """Index of the last bar that opened at or before t (or -1)."""
        return bisect.bisect_right(self._opens, t_ms) - 1

    def covers(self, t_ms: int) -> bool:
        i = self.bar_index_at(t_ms)
        return i >= 0 and t_ms <= self.bars[i].close_ms

    def price_at(self, t_ms: int) -> float | None:
        """Price at t: linear walk from open to close inside the bar; the last
        close between bars; None before the data starts or after it ends."""
        i = self.bar_index_at(t_ms)
        if i < 0:
            return None
        bar = self.bars[i]
        if t_ms > bar.close_ms:
            if i == len(self.bars) - 1 and t_ms - bar.close_ms > MAX_GAP_MS:
                return None
            return bar.c
        span = bar.close_ms - bar.open_ms + 1
        frac = (t_ms - bar.open_ms) / span
        return bar.o + (bar.c - bar.o) * frac

    def completed(self, now_ms: int, since_ms: int | None = None) -> list[Bar]:
        """Bars fully closed by now (no look-ahead), optionally from since."""
        end = bisect.bisect_right(self._opens, now_ms)
        out = [b for b in self.bars[:end] if b.close_ms < now_ms]
        if since_ms is not None:
            out = [b for b in out if b.open_ms >= since_ms]
        return out


def resample(bars: list[Bar], interval_ms: int, now_ms: int) -> list[Bar]:
    """Aggregate fine bars into coarser buckets; drop buckets still open."""
    buckets: dict[int, list[Bar]] = {}
    for b in bars:
        buckets.setdefault(b.open_ms - b.open_ms % interval_ms, []).append(b)
    out = []
    for start in sorted(buckets):
        end = start + interval_ms - 1
        if end >= now_ms:
            continue
        grp = buckets[start]
        out.append(
            Bar(
                start,
                end,
                grp[0].o,
                max(b.h for b in grp),
                min(b.l for b in grp),
                grp[-1].c,
                sum(b.v for b in grp),
            )
        )
    return out


class MarketData:
    """All loaded series, keyed by instrument ('BTCUSDT') then interval."""

    def __init__(self) -> None:
        self.series: dict[str, dict[str, Series]] = {}

    def add(self, series: Series) -> None:
        if len(series):
            self.series.setdefault(series.name, {})[series.interval] = series

    def instruments(self) -> list[str]:
        return sorted(self.series)

    def _finest(self, name: str) -> list[Series]:
        by_iv = self.series.get(name, {})
        return [by_iv[k] for k in sorted(by_iv, key=lambda k: INTERVAL_MS.get(k, DAY_MS))]

    def price(self, name: str, t_ms: int) -> float | None:
        for s in self._finest(name):
            if s.covers(t_ms):
                return s.price_at(t_ms)
        for s in self._finest(name):
            p = s.price_at(t_ms)
            if p is not None:
                return p
        return None

    def traded_between(self, name: str, lo_ms: int, hi_ms: int) -> bool | None:
        """Whether any bar of `name` overlaps [lo, hi]: True/False where some
        series spans that window, None where no series covers it."""
        answer = None
        for s in self._finest(name):
            if not s.bars or s.first_ms > lo_ms or s.last_ms < hi_ms:
                continue
            i = s.bar_index_at(hi_ms)
            if i >= 0 and s.bars[i].close_ms >= lo_ms:
                return True
            answer = False
        return answer

    def day_stats(self, name: str, t_ms: int) -> dict:
        """Open/high/low/volume of the UTC day so far, from the finest series."""
        day_start = t_ms - t_ms % DAY_MS
        for s in self._finest(name):
            if s.interval == "1d":
                continue
            bars = [b for b in s.completed(t_ms, day_start)]
            if bars:
                last = self.price(name, t_ms)
                return {
                    "open": bars[0].o,
                    "high": max(max(b.h for b in bars), last or bars[-1].c),
                    "low": min(min(b.l for b in bars), last or bars[-1].c),
                    "volume": sum(b.v for b in bars),
                }
        p = self.price(name, t_ms)
        return {"open": p, "high": p, "low": p, "volume": 0.0}

    def ohlcv(
        self,
        name: str,
        timeframe: str,
        now_ms: int,
        since_ms: int | None = None,
        limit: int = 100,
    ) -> list[list]:
        if timeframe not in INTERVAL_MS:
            raise ValueError(f"unsupported timeframe {timeframe!r}")
        by_iv = self.series.get(name, {})
        if timeframe in by_iv:
            bars = by_iv[timeframe].completed(now_ms, since_ms)
        else:
            finer = [
                s
                for s in self._finest(name)
                if INTERVAL_MS.get(s.interval, DAY_MS) < INTERVAL_MS[timeframe]
            ]
            if not finer:
                return []
            bars = resample(finer[0].completed(now_ms, since_ms), INTERVAL_MS[timeframe], now_ms)
        if since_ms is None:
            bars = bars[-limit:]
        else:
            bars = bars[:limit]
        return [b.as_ohlcv() for b in bars]


# ---------------------------------------------------------------- loaders


def _binance_ts(raw: str) -> int:
    """Binance spot files switched to microseconds on 2025-01-01."""
    v = int(raw)
    return v // 1000 if v > 10**14 else v


def parse_binance_klines(text: str) -> list[Bar]:
    bars = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].strip().isdigit():
            continue  # header line (futures files have one)
        bars.append(
            Bar(
                _binance_ts(row[0]),
                _binance_ts(row[6]),
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
            )
        )
    return bars


def _session_bounds(day: _dt.date, session: str) -> tuple[int, int]:
    if session == "us_equity":
        try:
            from zoneinfo import ZoneInfo

            ny = ZoneInfo("America/New_York")
            o = _dt.datetime.combine(day, _dt.time(9, 30), ny)
            c = _dt.datetime.combine(day, _dt.time(16, 0), ny)
            return int(o.timestamp() * 1000), int(c.timestamp() * 1000) - 1
        except Exception:  # no tz database: fall back to EDT hours
            base = int(_dt.datetime(day.year, day.month, day.day, tzinfo=_dt.timezone.utc).timestamp() * 1000)
            return base + 13 * 3_600_000 + 1_800_000, base + 20 * 3_600_000 - 1
    base = int(_dt.datetime(day.year, day.month, day.day, tzinfo=_dt.timezone.utc).timestamp() * 1000)
    return base, base + DAY_MS - 1


def parse_daily_csv(text: str, session: str = "utc_day") -> list[Bar]:
    """Daily OHLCV with a header such as Stooq's Date,Open,High,Low,Close,Volume."""
    reader = csv.DictReader(io.StringIO(text))
    cols = {k.lower(): k for k in (reader.fieldnames or [])}
    need = ["date", "open", "high", "low", "close"]
    if not all(n in cols for n in need):
        raise ValueError(f"daily CSV needs columns {need}, got {reader.fieldnames}")
    bars = []
    for row in reader:
        day = _dt.date.fromisoformat(row[cols["date"]][:10])
        o_ms, c_ms = _session_bounds(day, session)
        vol = row.get(cols.get("volume", ""), "") or 0
        bars.append(
            Bar(
                o_ms,
                c_ms,
                float(row[cols["open"]]),
                float(row[cols["high"]]),
                float(row[cols["low"]]),
                float(row[cols["close"]]),
                float(vol),
            )
        )
    return bars


def _read_payload(path: str) -> str:
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            return z.read(z.namelist()[0]).decode()
    with open(path, encoding="utf-8") as f:
        return f.read()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(manifest_path: str, cache_dir: str, only: Iterable[str] | None = None) -> MarketData:
    """Load every dataset in the manifest from the cache, checking hashes."""
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    wanted = set(only) if only else None
    md = MarketData()
    for ds in manifest["datasets"]:
        key = f"{ds['instrument']}@{ds['interval']}"
        if wanted is not None and key not in wanted and ds["instrument"] not in wanted:
            continue
        bars: list[Bar] = []
        for entry in ds["files"]:
            path = os.path.join(cache_dir, os.path.basename(entry["url"]))
            if not os.path.exists(path):
                raise FileNotFoundError(
                    f"{path} is missing; run `python -m brokersim fetch-data` first"
                )
            digest = sha256_file(path)
            if entry.get("sha256") and digest != entry["sha256"]:
                raise ValueError(f"{path}: sha256 {digest} != pinned {entry['sha256']}")
            text = _read_payload(path)
            if ds["format"] == "binance-kline":
                bars.extend(parse_binance_klines(text))
            elif ds["format"] == "daily-csv":
                bars.extend(parse_daily_csv(text, ds.get("session", "utc_day")))
            else:
                raise ValueError(f"unknown format {ds['format']!r}")
        md.add(Series(ds["instrument"], ds["interval"], bars))
    return md


def parse_bar_csv(text: str, interval: str, session: str = "utc_day") -> list[Bar]:
    """Bars with a ts_ms,open,high,low,close,volume header (fetch_market_data.py output).

    Daily bars with session "us_equity" span the regular session of their New
    York date, so a close is not visible before 16:00 ET that day.
    """
    reader = csv.DictReader(io.StringIO(text))
    step = INTERVAL_MS[interval]
    bars = []
    for row in reader:
        t = int(row["ts_ms"])
        if interval == "1d" and session == "us_equity":
            from zoneinfo import ZoneInfo

            day = _dt.datetime.fromtimestamp(t / 1000, ZoneInfo("America/New_York")).date()
            o_ms, c_ms = _session_bounds(day, session)
        else:
            o_ms, c_ms = t, t + step - 1
        bars.append(Bar(o_ms, c_ms, float(row["open"]), float(row["high"]), float(row["low"]),
                        float(row["close"]), float(row["volume"] or 0)))
    return bars


def load_bar_dir(md: MarketData, root: str, sessions: dict | None = None) -> None:
    """Load ROOT/<source>/<SYMBOL>/<interval>.csv as written by fetch_market_data.py.

    Every file listed in ROOT/manifest.json is checked against its SHA-256.
    Daily bars from "alpaca" are treated as US equity sessions unless
    `sessions` maps the source to something else.
    """
    sessions = {"alpaca": "us_equity", **(sessions or {})}
    with open(os.path.join(root, "manifest.json"), encoding="utf-8") as f:
        listed = json.load(f).get("files", {})
    for rel, meta in sorted(listed.items()):
        parts = rel.replace(os.sep, "/").split("/")
        interval = parts[-1].removesuffix(".csv")
        if len(parts) != 3 or interval not in INTERVAL_MS:
            continue  # e.g. funding_8h.csv
        path = os.path.join(root, *parts)
        digest = sha256_file(path)
        if meta.get("sha256") and digest != meta["sha256"]:
            raise ValueError(f"{path}: sha256 {digest} != manifest {meta['sha256']}")
        source, symbol = parts[0], parts[1]
        with open(path, encoding="utf-8") as fh:
            bars = parse_bar_csv(fh.read(), interval, sessions.get(source, "utc_day"))
        md.add(Series(symbol, interval, bars))


def load_local_csv(md: MarketData, instrument: str, path: str, session: str = "us_equity") -> None:
    """Add a user-supplied daily CSV (for example a Stooq download)."""
    md.add(Series(instrument, "1d", parse_daily_csv(_read_payload(path), session)))


def fetch_manifest(manifest_path: str, cache_dir: str, log=print) -> int:
    """Download every pinned file that is not already cached; verify SHA-256."""
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    os.makedirs(cache_dir, exist_ok=True)
    fetched = 0
    for ds in manifest["datasets"]:
        for entry in ds["files"]:
            path = os.path.join(cache_dir, os.path.basename(entry["url"]))
            if os.path.exists(path) and sha256_file(path) == entry.get("sha256"):
                continue
            log(f"fetching {entry['url']}")
            with urllib.request.urlopen(entry["url"], timeout=60) as resp:
                payload = resp.read()
            digest = hashlib.sha256(payload).hexdigest()
            if entry.get("sha256") and digest != entry["sha256"]:
                raise ValueError(f"{entry['url']}: sha256 {digest} != pinned {entry['sha256']}")
            with open(path, "wb") as f:
                f.write(payload)
            fetched += 1
    return fetched


def synthetic_series(instrument: str, start_ms: int, minutes: int, base: float) -> Series:
    """Deterministic made-up 1m bars (a slow sine wave) for tests and offline runs."""
    import math

    bars = []
    prev = base
    for i in range(minutes):
        t = start_ms + i * 60_000
        close = base * (1 + 0.01 * math.sin(i / 90.0) + 0.002 * math.sin(i / 7.0))
        hi, lo = max(prev, close) * 1.0004, min(prev, close) * 0.9996
        bars.append(Bar(t, t + 59_999, prev, hi, lo, close, 10.0))
        prev = close
    return Series(instrument, "1m", bars)


def synthetic_market(day_start_ms: int, days: int = 2) -> MarketData:
    md = MarketData()
    md.add(synthetic_series("BTCUSDT", day_start_ms, days * 1440, 100_000.0))
    md.add(synthetic_series("ETHUSDT", day_start_ms, days * 1440, 2_500.0))
    return md
