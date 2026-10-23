# vwap-penetration-momentum

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

When a bar closes beyond VWAP after a multi-bar drift phase, the close-to-VWAP distance relative to recent range predicts directional persistence on the next bar.

## The claim this makes testable

If a bar closes beyond VWAP with velocity (normalized distance) above a threshold, following a multi-bar drift, then the next bar's open-to-close return will be positive in the direction of the penetration more often than the long-term base rate.

## How to make it concrete

Measure drift phase duration, detect VWAP penetration, compute velocity as (|close − VWAP| / recent_range), enter on next bar's open if velocity exceeds threshold, exit on fixed holding period or adverse move.

## The precise specification

**Signal Definition:** drift_bars_t = count of consecutive bars where (high_t > VWAP_t AND low_t < VWAP_t) OR (close_t > VWAP_t AND close_{t-1} <= VWAP_{t-1}) OR (close_t < VWAP_t AND close_{t-1} >= VWAP_{t-1}); recent_range_t = max(high_{t-k}...high_t) − min(low_{t-k}...low_t) for k ∈ [5, 20]; velocity_t = |close_t − VWAP_t| / recent_range_t; penetration_direction_t = sign(close_t − VWAP_t); is_penetration_t = (close_t > VWAP_t AND close_{t-1} <= VWAP_{t-1}) OR (close_t < VWAP_t AND close_{t-1} >= VWAP_{t-1})
**Entry Rule:** (is_penetration_t = TRUE) AND (drift_bars_t >= min_drift_bars) AND (velocity_t >= velocity_threshold) THEN enter at the open of the next bar in direction of penetration_direction_t
**Exit Rule:** Exit at the open of the bar min(holding_period_bars, first bar where close crosses VWAP in opposite direction of entry, or adverse move exceeds 2 × recent_range_t)
**Bar Size:** 5m
**Session Filter:** all
**Cost Assumption:** fixed spread of 1 basis point per trade (entry + exit); no slippage model applied
**Data Fields Required:** close, high, low, volume, vwap
**Parameters:**
- min_drift_bars: min=2.0, max=6.0, step=1.0
- velocity_threshold: min=0.3, max=0.8, step=0.1
- recent_range_lookback: min=5.0, max=20.0, step=5.0
- holding_period_bars: min=2.0, max=10.0, step=2.0

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: VWAP field must be computed from intra-bar volume and price; bar-level VWAP is sufficient.

## Where it came from

- [VWAP Execution as an Optimal Strategy](http://arxiv.org/abs/1408.6118v4): VWAP is the optimal execution benchmark and reflects the volume-weighted fair price, making deviations from it meaningful for detecting execution-driven momentum.
- [Optimal VWAP execution under transient price impact](http://arxiv.org/abs/1901.02327v2): VWAP-based strategies must account for transient price impact and the timing of the trading interval relative to the benchmark, which aligns with multi-bar drift detection and velocity normalization.
- [VWAP execution and guaranteed VWAP](http://arxiv.org/abs/1306.2832v4): Pricing and trading performance around VWAP depends on distinguishing phases of drift and penetration, with execution costs and market impact modeled as continuous processes.

## Why it is worth a trial

VWAP is a well-established execution benchmark and fair-value anchor. A bar closing beyond VWAP after a multi-bar consolidation phase suggests either institutional execution completion or a shift in supply/demand, both of which can induce directional persistence. Normalizing the penetration magnitude by recent range controls for regime volatility. The mechanism is grounded in execution theory (sources 0, 1, 2) and avoids look-ahead bias by using only data through bar t to signal entry at t+1.
