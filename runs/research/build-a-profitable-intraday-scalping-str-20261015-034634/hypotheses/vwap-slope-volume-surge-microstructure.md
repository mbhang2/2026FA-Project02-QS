# vwap-slope-volume-surge-microstructure

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Order-flow clustering in spot precious metals creates temporary liquidity imbalances; steepening VWAP slope concurrent with volume surge signals arrival of informed flow, enabling entry before transient price acceleration reverses.

## The claim this makes testable

If VWAP slope steepens and volume surges together, then subsequent 5-minute bar returns will exhibit higher magnitude than periods when slope and volume move independently.

## How to make it concrete

Detect bars where VWAP slope acceleration and volume acceleration both exceed their respective rolling thresholds; enter at the open of the next bar; exit after a fixed holding period or on slope reversal detected in the current bar.

## The precise specification

**Signal Definition:** vwap_slope_t = (vwap_t - vwap_{t-2}) / vwap_{t-2}; vwap_accel_t = vwap_slope_t - vwap_slope_{t-1}; vol_accel_t = (volume_t - sma_vol_{t,n}) / sma_vol_{t,n}; sma_vol_{t,n} = mean(volume_{t-n+1:t}); signal_t = 1 if vwap_accel_t > threshold_accel AND vol_accel_t > threshold_vol else 0.
**Entry Rule:** Enter long at the open of the next bar if signal_t == 1 (i.e., both vwap acceleration and volume acceleration exceeded thresholds at bar t).
**Exit Rule:** Exit at the open of the bar that is hold_bars bars after entry, or immediately at the open of the current bar if vwap_accel_t < -threshold_accel (slope reversal detected in current bar before exit bar is reached).
**Bar Size:** 5m
**Session Filter:** all
**Cost Assumption:** Entry and exit at open price; assume 2 basis points round-trip slippage (entry at open + 1 bp, exit at open - 1 bp) to account for spot precious metals microstructure.
**Data Fields Required:** close, open, high, low, volume, vwap
**Parameters:**
- threshold_accel: min=0.0001, max=0.0008, step=0.0003
- threshold_vol: min=0.5, max=1.5, step=0.5
- sma_vol_window: min=5.0, max=15.0, step=5.0
- hold_bars: min=1.0, max=8.0, step=1.0

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: No external order-flow, bid/ask, or time-of-day data required. Mechanism operates purely on aggregated VWAP and volume fields available in 5-minute bar data.

## Where it came from

- [Deep Learning for VWAP Execution in Crypto Markets: Beyond the Volume Curve](http://arxiv.org/abs/2502.13722v2): VWAP is an optimal execution benchmark whose performance depends on the joint dynamics of volume and price; temporary deviations from the volume curve create trading edges.
- [Market Making in Spot Precious Metals](http://arxiv.org/abs/2404.15478v5): Spot precious metals liquidity is fragmented and exhibits multiple relaxation timescales; EFP spreads and order-flow clustering are key microstructure features.
- [VWAP Execution as an Optimal Strategy](http://arxiv.org/abs/1408.6118v4): VWAP execution is optimal for risk-neutral traders when volume processes are modeled explicitly, supporting the use of volume-weighted price dynamics as a signal.

## Why it is worth a trial

Spot precious metals exhibit lower tick frequency and higher relative spreads than equities, making microstructure-driven temporary imbalances more pronounced and longer-lived. The mechanism exploits the documented multi-timescale liquidity structure and EFP fragmentation; concurrent VWAP slope and volume acceleration are simple, causal features detectable from 5-minute bars. The hypothesis is grounded in order-flow clustering theory and does not require external data, making it feasible to test on available precious metals spot data. Grid size is 3 × 3 × 3 × 8 = 216 combinations.
