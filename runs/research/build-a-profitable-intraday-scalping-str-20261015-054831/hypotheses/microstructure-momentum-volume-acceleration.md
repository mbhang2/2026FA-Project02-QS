# microstructure-momentum-volume-acceleration

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Volume spikes combined with close-to-high positioning create short-term directional persistence due to microstructure imbalance before mean reversion.

## The claim this makes testable

IF volume exceeds its rolling mean by a threshold AND close is positioned in the upper half of the bar's range, THEN the next 2–5 bars exhibit directional continuation in the direction of the close relative to open, before reverting.

## How to make it concrete

Detect volume acceleration (vol_t > vol_mean_rolling × threshold) and close-to-high ratio (close_t - low_t) / (high_t - low_t) > 0.5, then measure forward returns over a 2–5 bar window.

## The precise specification

**Signal Definition:** vol_ratio_t = volume_t / mean(volume_{t-k:t-1}); close_to_high_t = (close_t - low_t) / (high_t - low_t); signal_t = 1 if (vol_ratio_t > vol_threshold AND close_to_high_t > 0.5 AND close_t > open_t) else -1 if (vol_ratio_t > vol_threshold AND close_to_high_t > 0.5 AND close_t < open_t) else 0.
**Entry Rule:** Enter long at the open of the next bar if signal_t == 1; enter short at the open of the next bar if signal_t == -1.
**Exit Rule:** Exit at the open of bar t + exit_bars, where exit_bars ∈ {2, 3, 4, 5}.
**Bar Size:** 5m
**Session Filter:** all
**Cost Assumption:** round-trip slippage of 1 basis point per entry and exit (0.5 bp entry, 0.5 bp exit) applied to entry price.
**Data Fields Required:** close, high, low, open, volume
**Parameters:**
- vol_lookback_k: min=5.0, max=20.0, step=5.0
- vol_threshold: min=1.2, max=2.0, step=0.2
- exit_bars: min=2.0, max=5.0, step=1.0
- close_to_high_min: min=0.5, max=0.7, step=0.1

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: No external data required; mechanism is purely bar-level.

## Where it came from

- [Flow Toxicity and Liquidity in a High Frequency World](https://www.stern.nyu.edu/sites/default/files/assets/documents/con_035928.pdf): Volume imbalance and order flow toxicity are measurable short-term drivers of directional volatility and price movement.

## Why it is worth a trial

Microstructure momentum is a well-documented phenomenon in high-frequency settings. Volume acceleration paired with close positioning is a simple, implementable proxy for order imbalance. The 2–5 bar window targets the persistence phase before mean reversion, a regime where microstructure effects dominate. The mechanism uses only OHLCV data available at the 5-minute bar level, making it directly testable without tick-level granularity.
