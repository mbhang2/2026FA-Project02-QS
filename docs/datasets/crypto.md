# Crypto perpetuals: Binance, Bybit, OKX

Raw public archives for USDT-margined perpetual futures (BTCUSDT and ETHUSDT by
default), fetched by `scripts/crypto_fetch.py` and read by
`src/core/data/crypto_raw.py`.

**Binance is served to campaigns** by `src/core/data/crypto.py` (`crypto-trades:BTCUSDT`,
`crypto-trades:ETHUSDT`; datasets `crypto-btc`, `crypto-eth` and profiles
`crypto-btc-train` / `crypto-btc-validation` in `config/run.yaml`). The split is
pre-registered in `config/calendars/crypto-trades.yaml`, and costs and scoring follow
pre-registration **Amendment 16 (in force from 2026-11-19)**: 5 bp of notional
per side, returns on each session's opening notional, an idle charge of 1.2e-5 of
notional per session, and funding paid on the position held at each settlement.
Bybit and OKX are downloaded but not yet served.

| | |
|---|---|
| session | a UTC day, 00:00-24:00, one archive file; no holidays, no early closes |
| size | integer lots of 0.001 coin (Binance's quantity step); asserted whole, never rounded |
| aggressor | always known, so `bar.delta` is exact |
| P&L | price points x lots; times 0.001 for USD |
| decoded cache | under `data/cache/` (gitignored, in `crypto-sessions/`), ~5 MB per 2023 session and several times that in 2025-26; the train split needs roughly 10-15 GB |

[Data and environment](../guide/data-and-environment.md) · config: `config/datasets/crypto.yaml` · record: `data/crypto-manifest.json`

## No API key

Every source is public. None of it needs an account:

| Exchange | Index | Files | Kinds |
|---|---|---|---|
| Binance | S3 listing of `data.binance.vision` | `data.binance.vision/data/futures/um/...` | `trades` (aggTrades, daily), `klines_1m` (daily), `funding` (monthly), `metrics` (daily: open interest, long/short ratios, 5 min) |
| Bybit | directory listing of `public.bybit.com/trading/<SYMBOL>/` | same | `trades` (daily) |
| OKX | `GET /api/v5/public/market-data-history` | `static.okx.com/cdn/okex/traderecords/...` | `trades` (daily), `klines_1m` (daily), `funding` (monthly) |

**Bybit funding is missing.** Its only source is the v5 REST API at
`api.bybit.com`, which returns a CloudFront geo-block from the United States.
The trade archive at `public.bybit.com` is not blocked.

**Monthly funding lags.** Binance and OKX publish a month's funding file after the
month ends, so the current month is never there.

## Conventions that differ, and how the reader resolves them

| | Binance | Bybit | OKX |
|---|---|---|---|
| aggressor | `is_buyer_maker=true` means the **seller** crossed | `side` = taker side | `side` = taker side |
| timestamp | ms UTC | **seconds**, decimal string (parsed exactly, not via float) | ms UTC |
| size | base coin | base coin | **contracts**: times `ctVal` (BTC 0.01, ETH 0.1) |
| a daily file is | a UTC day | a UTC day, **rows descending** in older years | a **UTC+8 day**: 16:00 UTC to 16:00 UTC |
| header row | missing in some early files | present | present |

`read_trades` returns `ts_ns, price, size, side, trade_id`, sorted by time, with
`side` +1 when the buyer was the aggressor. Every trade on all three exchanges has
a known aggressor, so unlike NQ there is no unknown-side volume and CVD is exact,
not inferred. Binance klines carry taker-buy volume; OKX klines do not
(`buy_volume` is NaN), so OKX order flow has to come from its trades.

Re-bucket by `ts_ns`, never by file: an OKX "2026-11-17" file starts at
2026-11-16 16:00 UTC.

**Known defects in the archives, checked 2026-11-19 over 2023-01-01 to 2026-11-18.**
Binance 1-minute klines are complete for both symbols: 2,041,920 bars, no
duplicates, no missing minute. OKX klines repeat 56 bars as identical rows (in the
files for 2023-10-12 and 2024-06-25; the reader drops them) and are missing 22:17
and 22:18 UTC on 2023-06-22. No exchange skipped a day.

OKX's `ctVal` is checked against `/api/v5/public/instruments` on every fetch and
recorded in the manifest under `instruments`. If it disagrees with the config, the
fetch stops instead of scaling volume wrong.

## Fetching

```bash
python scripts/crypto_fetch.py plan                        # listed, missing, size, free space
python scripts/crypto_fetch.py fetch --kind klines_1m --kind funding --kind metrics
python scripts/crypto_fetch.py fetch --exchange binance --kind trades --start 2026-01-01 --max-gib 5
python scripts/crypto_fetch.py fetch --to-r2 --drop-local  # stream everything to R2
python scripts/crypto_fetch.py upload --drop-local         # push what is already on disk
python scripts/crypto_fetch.py verify
```

- **Where files go.** `~/dl/crypto` (or `$CRYPTO_LAKE`) at
  `crypto/raw/<exchange>/<kind>/<symbol>/<upstream filename>`. A lake root inside
  the repository is refused.
- **Verification.** Binance's published SHA-256 must match. Bybit's file must match
  its Content-Length and gunzip to the end. OKX publishes no checksum, so its zip
  must pass a CRC check. A file is written under its real name only after it
  verifies.
- **The manifest** (`data/crypto-manifest.json`, committed) records size, SHA-256,
  source URL, how the file was verified, when it was fetched, and the R2 bucket
  that holds it. It also records, per exchange, kind and symbol, the coverage and
  any day the exchange did not publish (`upstream_gaps`).
- **Disk.** Trades are the bulk. In 2026, BTC runs about 20 MB a day on Binance
  and OKX and 80 MB on Bybit; ETH is similar. That is roughly 0.25 GB per day for
  both symbols on all three exchanges. `fetch` stops before free space falls below
  `--reserve-gib` (default 5).

## R2

`--to-r2` uploads each verified file to the same key, stores its SHA-256 as
object metadata, and checks the stored size and hash before recording the bucket.
With `--drop-local`, the local copy is deleted once R2 holds it, so the full
history passes through a small disk. Credentials come from the `r2w` remote in
`secrets/rclone.conf` (the read-write key; `--r2-remote` picks another), else from
`R2_ENDPOINT`, `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`. The bucket is
`CRYPTO_R2_BUCKET`, else `R2_BUCKET`, else the one in `secrets/r2.env`. The
personal read-only keys in [setup/R2_ACCESS.md](../setup/R2_ACCESS.md) can read
the files but cannot upload. The key is the same locally and in R2:
`crypto/raw/<exchange>/<kind>/<symbol>/<file>`.

## Trading access, separately from the data

The data is public. Trading is not: Binance.com, Bybit and OKX's global platform
do not accept US residents for perpetual futures. A strategy found on this data
is evidence about the market, but a live track record from the United States
would have to come from a venue that admits US persons.
