# pm-futures-intraday-momentum-persistence

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Precious metals futures exhibit directional continuation within sub-hourly windows because order flow clustering and EFP-driven hedging flows create autocorrelated momentum that decays over 1–4 bars rather than reversing immediately.

## The claim this makes testable

If intraday momentum in precious metals futures persists over the subsequent hourly window, then the sign of cumulative returns over bars t to t+k will match the sign of returns over bars t−m to t−1 more often than a random walk.

## How to make it concrete

Compute lagged returns over a lookback window; enter long/short when recent momentum exceeds a percentile threshold; hold for a fixed number of bars and measure whether directional continuation occurs.

## The precise specification

**Signal Definition:** r_t = (C_t − C_{t−1}) / C_{t−1}; momentum_signal = sum(r_{t−m} to r_{t−1}) where m ∈ [2, 6] bars; threshold_pct = percentile(momentum_signal, p) where p ∈ [60, 85]; entry_signal = 1 if momentum_signal > threshold_pct, −1 if momentum_signal < (1 − p)-th percentile, else 0.
**Entry Rule:** Enter long at close of bar t if entry_signal = 1; enter short at close of bar t if entry_signal = −1; no entry if entry_signal = 0.
**Exit Rule:** Exit at close of bar t + h where h ∈ [2, 8] (holding period in bars); no profit target or stop-loss.
**Bar Size:** 5m
**Session Filter:** all
**Cost Assumption:** Assume round-trip cost of 0.5 basis points (0.005%) per trade, deducted from entry and exit fills; no slippage beyond this fixed spread.
**Data Fields Required:** close
**Parameters:**
- lookback_bars: min=2.0, max=6.0, step=1.0
- percentile_threshold: min=60.0, max=85.0, step=5.0
- holding_period_bars: min=2.0, max=8.0, step=1.0

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: Only close price required; no limit order book, no tick-level data, no bid–ask spreads.

## Where it came from

- [Market Making in Spot Precious Metals](http://arxiv.org/abs/2404.15478v5): EFP spreads in precious metals futures exhibit multiple modes of relaxation corresponding to diverse trading horizons, suggesting that order flow effects persist over sub-hourly windows.

## Why it is worth a trial

Precious metals futures are driven by both hedging flows (EFP arbitrage) and directional trading; sub-hourly momentum may persist due to clustering of order flow around risk management windows and real-time forecast updates, analogous to the role of continuous intraday markets in mitigating imbalances. The mechanism is testable with 5-minute bars and requires no tick or spread data.
