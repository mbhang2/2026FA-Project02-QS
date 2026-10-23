# microstructure-momentum-5m-open-imbalance

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Overnight order imbalance creates directional clustering in liquidity at market open; the first few 5-minute bars exhibit momentum continuation as accumulated overnight orders execute and attract follow-on flow.

## The claim this makes testable

IF overnight close-to-open gap direction is followed by elevated volume and positive correlation between open-to-close returns in the first N bars post-open, THEN cumulative returns in those bars will trend in the gap direction more often than in mean-reversion direction.

## How to make it concrete

Measure overnight gap as (open_bar1 - close_bar0) / close_bar0; compute rolling volume ratio and intra-bar return correlation; long position if gap and volume signal align; exit after N consecutive bars.

## The precise specification

**Signal Definition:** gap_t = (open_t - close_{t-1}) / close_{t-1}; vol_ratio_t = volume_t / SMA(volume, lookback=20); return_t = (close_t - open_t) / open_t; momentum_signal_t = sign(gap_t) * (1 if vol_ratio_t > vol_threshold else 0)
**Entry Rule:** At the open of bar t (where t is the first bar of the session), if abs(gap_t) > gap_threshold AND vol_ratio_t > vol_threshold, enter long if gap_t > 0, enter short if gap_t < 0. Entry fills at open of bar t.
**Exit Rule:** Close position at the open of bar (t + hold_bars), where hold_bars is a parameter. Alternatively, exit if intra-bar return reverses by more than reversal_threshold within the hold window.
**Bar Size:** 5m
**Session Filter:** all
**Cost Assumption:** Assume 1.5 basis points round-trip slippage per trade (entry and exit), applied symmetrically to all fills.
**Data Fields Required:** open, close, volume
**Parameters:**
- gap_threshold: min=0.0005, max=0.003, step=0.0005
- vol_threshold: min=1.1, max=1.8, step=0.2
- hold_bars: min=1.0, max=5.0, step=1.0
- lookback: min=10.0, max=30.0, step=5.0

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: Requires bar index to identify session start (first bar of trading session); no external order flow, bid/ask, or tick-level data needed.

## Where it came from

- [Short-horizon mean reversion in cryptocurrency markets - arXiv](https://arxiv.org/html/2608.21888v1): Short-horizon reversals concentrate after aggressive taker-driven moves and scale with flow intensity, suggesting that directional flow clustering in early bars post-open can predict near-term continuation.
- [Predicting Commodity Returns Through Image-Based Price Patterns](https://onlinelibrary.wiley.com/doi/10.1002/fut.70043): OHLC-based price pattern extraction enables predictability of returns over short horizons, consistent with the use of open-high-low-close data to identify microstructure-driven directional regimes.

## Why it is worth a trial

The hypothesis rests on the empirical observation that overnight order imbalance and liquidity clustering at market open create measurable directional bias in the first bars. The mechanism is grounded in microstructure theory: gap direction signals the net overnight demand shock, elevated volume indicates execution of accumulated orders, and momentum continuation reflects follow-on flow. The 5-minute bar frequency is coarse enough to avoid tick-level noise but fine enough to capture intra-session momentum before mean reversion dominates. The grid size (4 parameters × ~100 combinations) remains computationally tractable and avoids overfitting. Prior work on short-horizon flow-driven reversals and OHLC-based pattern recognition supports the plausibility of this mechanism.
