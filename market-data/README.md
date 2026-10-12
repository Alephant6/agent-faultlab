# market-data

Downloads the historical prices that [brokersim](../brokersim/README.md)
replays. Standard library only (Python 3.8+). The data itself is not in this
repository: each user downloads it under the publisher's terms.

## Scope

| item | value | why |
|---|---|---|
| pinned "today" | 2025-06-13 (a Friday) | BTC moved from about 105.7k down to 102.7k and back to 106.1k that day (UTC daily bar), so prices actually move during a test |
| 1-minute bars | 2025-06-09 to 2025-06-20 | Alva's data skill asks for the latest price from minute bars, and the broker's price collar (about 12%) needs a reference price at order time. Two weeks cover repeated scheduled runs. 2025-06-19 (Juneteenth) has US stocks closed and crypto open |
| daily bars | 2024-06-01 to 2025-06-30 | about a year of history for a 200-day average or a one-year backtest |
| crypto | BTCUSDT, ETHUSDT spot, plus USD-M perpetual funding rates for June 2025 | the broker has a `funding-rate` read |
| US stocks | AAPL, NVDA, SPY | two liquid large caps (NVDA moves a lot) and SPY as a benchmark |

Widen it with `--crypto`, `--stocks`, `--replay START END` and
`--lookback START END`.

## Usage

```bash
# crypto: no key needed
python3 -I fetch_market_data.py --out data --only binance

# US stocks: a free Alpaca key, from the environment only
export APCA_API_KEY_ID=... APCA_API_SECRET_KEY=...
python3 -I fetch_market_data.py --out data --only alpaca
```

Output: `data/<source>/<SYMBOL>/{1m,1d,funding_8h}.csv` with columns
`ts_ms,ts_utc,open,high,low,close,volume,trades,vwap` (UTC, milliseconds;
Binance's 2025 files use microseconds and are converted). `data/manifest.json`
records each file's source URLs, row count and SHA-256; runs with different
`--only` values merge into it. brokersim loads this directory with
`"data": {"manifest": null, "bar_dir": "<path to data>"}` and checks every file
against the manifest.

`reference-manifest.json` is the manifest of our own crypto download
(2026-10-12). Compare its hashes with yours to confirm you replay the same
prices. Alpaca hashes are not included: they depend on the feed (an account
without a SIP subscription gets 403 on `sip`, and the script falls back to the
free IEX feed) and on later revisions by Alpaca.

## Sources

| source | used for | terms |
|---|---|---|
| [Binance Vision](https://data.binance.vision/) bulk files | crypto klines and funding rates; no key, each file has a `.CHECKSUM` | CC BY-NC-SA 4.0 under the [Binance Vision Dataset Terms v1.0 (2026-08-26)](https://data.binance.vision/Binance_Vision-Terms_of_Use.pdf), which allow non-commercial open-source evaluation, require attribution to Binance Vision and forbid implying endorsement |
| [Alpaca Market Data v2](https://docs.alpaca.markets/docs/about-market-data-api) | US stock 1-minute and daily bars | free key required; we found no public redistribution terms, so the data is treated as personal use and never committed |

Notes from our runs:

* `api.binance.com` returns HTTP 451 from US addresses; the bulk file site
  does not, and the simulator never calls a live API.
* The IEX feed only has a bar for minutes with an IEX trade (about 380 to 394
  a day instead of 390). brokersim holds the last close between bars.
* NVDA includes a few pre- and post-market bars; the regular session is
  13:30 to 19:59 UTC in June.
* Alpaca stamps daily bars at New York midnight (04:00Z in summer); brokersim
  places them on the 09:30 to 16:00 session of that date.
* Alpaca's own crypto bars were too sparse (8,918 of 17,280 minutes for
  BTC/USD over two weeks), so crypto comes from Binance.

Crypto data: Binance Vision, CC BY-NC-SA 4.0. Neither Binance nor Alpaca
endorses this project.
