# intraday-vwap-mean-reversion-volatility-bands

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Intraday mean reversion arises when price deviates from session VWAP by more than one volatility unit; the reversion tendency is exploited by fading the divergence and exiting on return toward VWAP.

## The claim this makes testable

IF price closes more than k volatility units away from VWAP within a session, THEN the next bar's close will be closer to VWAP than the current bar's close.

## How to make it concrete

Compute intrabar deviation from VWAP in units of rolling volatility; trigger entry when deviation exceeds threshold k; exit when price reverts toward VWAP or session ends.

## The precise specification

**Signal Definition:** dev_t = (close_t - vwap_t) / vol_t, where vol_t = sqrt(mean((close_t - close_{t-1})^2, window=w)); signal_t = 1 if dev_t > k, -1 if dev_t < -k, else 0.
**Entry Rule:** Enter long if signal_t = -1 (price below VWAP by k volatility units) at the close of bar t; enter short if signal_t = 1 (price above VWAP by k volatility units) at the close of bar t. Order fills at the open of the next bar.
**Exit Rule:** Exit long position if (close_t - vwap_t) > -0.5 * vol_t OR bar index reaches session end; exit short position if (close_t - vwap_t) < 0.5 * vol_t OR bar index reaches session end.
**Bar Size:** 5m
**Session Filter:** all
**Cost Assumption:** Fixed round-trip cost of 2 basis points (spread + slippage) per trade.
**Data Fields Required:** close, vwap, volume
**Parameters:**
- k: min=0.5, max=2.5, step=0.5
- w: min=5.0, max=20.0, step=5.0
- exit_threshold: min=0.25, max=0.75, step=0.25

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: Session boundaries (intraday only) derived from bar index.

## Where it came from

- [Volume Weighted Average Price (VWAP) — Indicators and Strategies](https://www.tradingview.com/scripts/vwap/): VWAP bands measure price deviation from session VWAP in units of recent volatility, forming a standard intraday benchmark for fair value.
- [Mean Reversion Strategies: Backtested Trading Rules](https://www.quantifiedstrategies.com/mean-reversion-strategies/): Mean reversion strategies exploit price divergences by fading extremes and exiting on reversion to equilibrium levels.
- [VWAP Trading Strategy Explained | Capital.com UAE](https://capital.com/en-ae/learn/technical-analysis/volume-weighted-average-price-vwap-indicator): VWAP is used as a reference for intraday fair value; deviations from VWAP signal potential reversion opportunities.

## Why it is worth a trial

VWAP is a well-established intraday fair-value benchmark; mean reversion around anchors is a documented market microstructure phenomenon. The mechanism is simple, causal, and implementable on 5-minute bar data. The hypothesis is grounded in the principle that extreme deviations from volume-weighted price attract counter-flow, creating testable reversion patterns within a session.
