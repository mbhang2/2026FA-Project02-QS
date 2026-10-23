# vwap-slope-volume-concentration-momentum

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Intraday trend intensity rises when VWAP slope steepens concurrent with above-median volume concentration, signaling acceleration of price discovery beyond typical intraday range.

## The claim this makes testable

When VWAP slope magnitude exceeds a rolling percentile threshold AND volume concentration (current bar volume relative to rolling mean) exceeds a second threshold simultaneously, subsequent 5-minute bar close will be directionally aligned with the VWAP slope direction more often than a random baseline.

## How to make it concrete

Compute VWAP slope over a short rolling window, measure volume concentration as ratio of current bar volume to trailing mean, trigger entry when both slope magnitude and volume ratio exceed independent thresholds, hold for a fixed bar count.

## The precise specification

**Signal Definition:** vwap_slope_t = (vwap_t - vwap_{t-n_slope}) / vwap_{t-n_slope}; vol_concentration_t = volume_t / mean(volume_{t-n_vol:t-1}); slope_threshold_t = percentile(|vwap_slope_{t-n_lookback:t}|, pct_slope); vol_threshold_t = percentile(vol_concentration_{t-n_lookback:t}, pct_vol)
**Entry Rule:** IF |vwap_slope_t| > slope_threshold_t AND vol_concentration_t > vol_threshold_t AND |vwap_slope_t| > 0 THEN signal = sign(vwap_slope_t) ELSE signal = 0
**Exit Rule:** Close position at the open of the bar n_hold bars after entry, or immediately if signal flips to opposite direction
**Bar Size:** 5m
**Session Filter:** all
**Cost Assumption:** 0.5 basis points round-trip slippage applied to entry and exit fills
**Data Fields Required:** close, vwap, volume
**Parameters:**
- n_slope: min=2.0, max=6.0, step=1.0
- n_lookback: min=10.0, max=30.0, step=5.0
- pct_slope: min=60.0, max=85.0, step=5.0
- pct_vol: min=60.0, max=80.0, step=5.0

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: Requires rolling window statistics (percentile, mean) computed over trailing bars; no external data required

## Where it came from

- [VWAP Execution as an Optimal Strategy](http://arxiv.org/abs/1408.6118v4): VWAP is the standard benchmark for trade execution and reflects the aggregation of volume and price dynamics over an intraday window.
- [Deep Learning for VWAP Execution in Crypto Markets: Beyond the Volume Curve](http://arxiv.org/abs/2502.13722v2): Volume curve prediction and price movement interact dynamically in intraday trading, with volatility regimes affecting the relationship between volume concentration and price acceleration.
- [Free Stock Screeners, Charts, Technical Analysis](https://charted.market/about): Intraday momentum and volume dynamics are measurable features used to detect trend strength and reversals.

## Why it is worth a trial

VWAP slope captures directional price discovery intensity, while volume concentration measures market participation clustering. When both rise together, they indicate acceleration beyond typical intraday noise, offering a testable joint condition. The mechanism is grounded in execution theory (source [0]) and intraday feature detection (source [5]), and exploits the natural comovement of volume and momentum documented in cross-sectional studies (source [3]). The 5-minute bar frequency allows sufficient granularity to detect intraday acceleration while avoiding tick-level noise. Grid size is 4 × 6 × 5 × 4 = 480 combinations, well within budget.
