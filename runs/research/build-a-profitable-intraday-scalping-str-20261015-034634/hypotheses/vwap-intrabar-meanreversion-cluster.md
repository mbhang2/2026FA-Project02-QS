# vwap-intrabar-meanreversion-cluster

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Intraday mean reversion to VWAP within single 5-minute bars: when price deviates from VWAP by a threshold magnitude and closes near the bar anchor (open or prior VWAP), a directional reversal signal forms. The mechanism predicts that the next bar will exhibit price movement opposite to the current bar's deviation direction.

## The claim this makes testable

If price deviation from VWAP exceeds a threshold AND bar close proximity to anchor is tighter than a second threshold, then the next bar's open-to-close return will be opposite in sign to the current bar's close-to-VWAP deviation.

## How to make it concrete

Compute intrabar VWAP deviation as (close - vwap) / vwap; flag bars where |deviation| > threshold_A and (close - open) / open is within ±threshold_B of zero; enter short if deviation > 0 and long if deviation < 0 at the open of the next bar.

## The precise specification

**Signal Definition:** dev_t = (close_t - vwap_t) / vwap_t; anchor_proximity_t = |close_t - open_t| / open_t; signal_t = sign(dev_t) if |dev_t| > threshold_dev AND anchor_proximity_t < threshold_anchor else 0
**Entry Rule:** Enter at the open of the next bar (bar t+1) with position sign = −sign(dev_t) if signal_t ≠ 0; long if dev_t < −threshold_dev, short if dev_t > threshold_dev
**Exit Rule:** Exit at the open of bar t+2 (two bars after signal bar); fixed 1-bar holding period from entry
**Bar Size:** 5m
**Session Filter:** all
**Cost Assumption:** Round-trip slippage of 1.5 basis points (0.00015 in fractional terms) applied to entry and exit fills
**Data Fields Required:** close, open, high, low, volume, vwap
**Parameters:**
- threshold_dev: min=0.001, max=0.01, step=0.002
- threshold_anchor: min=0.001, max=0.008, step=0.002

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: Mechanism requires VWAP calculation over the 5-minute bar; assumes VWAP field is provided by data harness

## Where it came from

- [VWAP Execution as an Optimal Strategy](http://arxiv.org/abs/1408.6118v4): VWAP is the optimal execution benchmark for volume-weighted trading strategies, establishing VWAP as a natural anchor for price behavior analysis.
- [Hidden Order in Trades Predicts the Size of Price Moves](http://arxiv.org/abs/2512.15720v1): Price move magnitude exhibits systematic predictable structure at intraday horizons, supporting the premise that deviation magnitude contains exploitable signal.

## Why it is worth a trial

Mean reversion within single bars is a canonical microstructure phenomenon; VWAP as an intrabar anchor is theoretically justified by optimal execution theory. The mechanism isolates bars where price overshoots VWAP but remains anchored near bar open, a setup that should exhibit reversion over the next bar. Parameter count is minimal (2 thresholds) and grid is small (~25 combinations). This is a direct, falsifiable test of whether VWAP deviation plus anchor proximity jointly predict next-bar reversal.
