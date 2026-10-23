# volatility-regime-filter-mean-reversion

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Mean-reversion edge strengthens when intrabar range contracts below its median, filtering out false breakouts and improving entry precision by suppressing noise-driven reversals.

## The claim this makes testable

IF recent intrabar range falls below its median AND a price reversal signal fires AND price deviates beyond a K-period moving average, THEN the absolute close-to-close return magnitude on the next bar will be larger when range is below median than when range is at or above median, measured as a difference in median return magnitude across the two regimes.

## How to make it concrete

Compute rolling median of intrabar range (high − low) over N bars; trigger mean-reversion entry only when current range falls below that median and price closes beyond a K-period moving average; measure next-bar close-to-close return magnitude under low-range vs. high-range regimes and compare distributions.

## The precise specification

**Signal Definition:** range_t = high_t − low_t; median_range_t = median(range_{t−N+1}...range_t); is_low_vol = (range_t < median_range_t); deviation_t = close_t − sma_K_t where sma_K_t = mean(close_{t−K+1}...close_t); reversal_signal = sign(close_{t−1} − close_{t−2}) ≠ sign(close_t − close_{t−1})
**Entry Rule:** (is_low_vol == true) AND (reversal_signal == true) AND (|deviation_t| > threshold_pct × sma_K_t)
**Exit Rule:** Exit at the open of the next bar if M bars have elapsed since entry, or if close_t crosses sma_K_t in the direction opposite to the entry signal.
**Bar Size:** 5m
**Session Filter:** all
**Cost Assumption:** Fixed 2 basis points round-trip (entry + exit); no slippage model applied beyond market open fill assumption.
**Data Fields Required:** close, high, low
**Parameters:**
- N_lookback_range_median: min=10.0, max=50.0, step=10.0
- K_sma_period: min=5.0, max=20.0, step=5.0
- threshold_pct_deviation: min=0.001, max=0.01, step=0.003
- M_hold_bars: min=1.0, max=10.0, step=3.0

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: No external series required; all features derived from OHLC bars.

## Where it came from

- [Building a Robust Trading Pipeline with Python and TA-Lib - Medium](https://medium.com/@writeronepagecode/engineering-a-stock-prediction-pipeline-building-a-robust-trading-pipeline-with-python-and-ta-lib-8a72400ef726): Volatility filters and range-based measures serve to distinguish mean-reversion regimes from breakout regimes.
- [Contrarian — المؤشرات والاستراتيجيات — TradingView](https://ar.tradingview.com/scripts/contrarian/): Contrarian strategies employ deviation bands whose width varies inversely with short-term volatility to filter false extremes.

## Why it is worth a trial

The mechanism rests on a well-documented principle: mean-reversion is a genuine edge under low-noise (low-volatility) regimes, while high noise suppresses it. By gating entry only when intrabar range contracts below its own median, the filter reduces whipsaw risk and aligns order flow with the latent mean. The 5-minute bar frequency and simple OHLC inputs make the hypothesis tractable and reproducible without exotic data.
