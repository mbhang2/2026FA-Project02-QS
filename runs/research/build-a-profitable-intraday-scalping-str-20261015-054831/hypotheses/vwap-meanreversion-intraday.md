# vwap-meanreversion-intraday

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Price excursions beyond VWAP by a threshold multiple of recent bar volatility trigger mean-reversion trades that close within the same session as the anchor bar's VWAP drifts back into the excursion zone.

## The claim this makes testable

If price exceeds VWAP by more than K volatility multiples, then the absolute return from entry to exit within the same session will be lower in magnitude than the absolute return from VWAP to the excursion point.

## How to make it concrete

Anchor VWAP at bar t; compute rolling volatility from recent closes; trigger entry when close deviates from VWAP by K × volatility; exit at end of session or when price returns within M × volatility of VWAP.

## The precise specification

**Signal Definition:** vol_t = stdev(close[t-N:t], N); vwap_anchor = vwap[t]; excursion_upper = vwap_anchor + K × vol_t; excursion_lower = vwap_anchor - K × vol_t; reversion_band_upper = vwap_anchor + M × vol_t; reversion_band_lower = vwap_anchor - M × vol_t; signal_long = 1 if close[t] < excursion_lower else 0; signal_short = 1 if close[t] > excursion_upper else 0
**Entry Rule:** Enter long at the open of bar t+1 if signal_long[t] = 1 and bar t is not the final bar of the session. Enter short at the open of bar t+1 if signal_short[t] = 1 and bar t is not the final bar of the session.
**Exit Rule:** Exit long if close[t] >= reversion_band_lower or at the open of the final bar of the session, whichever comes first. Exit short if close[t] <= reversion_band_upper or at the open of the final bar of the session, whichever comes first.
**Bar Size:** 5m
**Session Filter:** all
**Cost Assumption:** Fixed 1 basis point round-trip slippage (entry and exit combined) to account for bid-ask spread and execution friction on 5-minute bars.
**Data Fields Required:** close, high, low, vwap, volume
**Parameters:**
- N: min=4.0, max=20.0, step=4.0
- K: min=1.0, max=3.0, step=0.5
- M: min=0.25, max=0.75, step=0.25

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: Session boundary detection derived from bar index; no external data required.

## Where it came from

- [VWAP Execution as an Optimal Strategy](http://arxiv.org/abs/1408.6118v4): VWAP is an optimal execution benchmark and institutional anchor point for price trajectories within a trading session.

## Why it is worth a trial

VWAP anchors are widely used in institutional trading and serve as a natural reference level. Intraday mean-reversion is a well-documented microstructure phenomenon; anchoring it to volatility-normalized deviations from VWAP provides a testable framework for capturing snapback moves within a single session. The mechanism is small, causal, and uses only bar-level data available in the harness.
