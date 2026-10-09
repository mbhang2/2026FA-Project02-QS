# intraday-momentum-high-volume-intervals

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

During intervals of elevated intraday volume, momentum measured over recent 5-minute bars predicts the direction of returns in the subsequent bar, because high-volume intervals concentrate informed trading that extends price trends.

## The claim this makes testable

If volume in the current bar exceeds the 70th percentile of the rolling 20-bar volume distribution, then the sign of the 3-bar returns momentum will predict the sign of the next bar's return with directional accuracy above 51%.

## How to make it concrete

Construct a volume percentile rank over a rolling window; segment bars into high-volume and normal-volume regimes; compute lagged momentum as the sum of recent log returns; model the relationship between regime, momentum, and forward return direction.

## The precise specification

Let V_t = volume of bar t. Let V_pct_t = percentile_rank(V_t, [V_{t-19}, ..., V_t]) over the most recent 20 bars. Define high_volume_regime_t = 1 if V_pct_t >= 70, else 0. Let r_t = ln(C_t / C_{t-1}) where C_t is the close of bar t. Define momentum_t = r_{t-1} + r_{t-2} + r_{t-3} (sum of log returns over the three preceding bars). Define forward_return_t = ln(C_{t+1} / C_t). Entry condition: high_volume_regime_t == 1 AND |momentum_t| > 0 (momentum is nonzero). Prediction direction: sign(momentum_t). Exit condition: at the close of bar t+1 (hold for exactly one bar). Target: P(sign(forward_return_t) == sign(momentum_t) | high_volume_regime_t == 1) > 0.51. Parameters: volume_percentile_threshold = 70 (range: 60–80); momentum_lookback = 3 bars (range: 2–5); volume_window = 20 bars (range: 10–40). Evaluation metric: directional accuracy (fraction of trades where predicted direction matches realized direction sign) computed separately within high-volume and normal-volume subsamples.

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: OHLCV bars; no tick-level or bid-ask data required

## Where it came from

- [Sequential Structure in Intraday Futures Data: LSTM vs Gradient Boosting on MNQ](http://arxiv.org/abs/2605.17724v1): Five-minute OHLCV bar sequences in single-instrument equity index futures datasets contain exploitable sequential structure detectable by machine learning models under expanding-window evaluation.
- [Forecasting Intraday Volume in Equity Markets with Machine Learning](http://arxiv.org/abs/2505.08180v1): Intraday trading volume is highly predictable using machine learning and commonality features, with economic benefits for portfolio implementation and trade execution.

## Why it is worth a trial

Volume regimes are known to correlate with order flow intensity and information arrival in equity markets. Momentum persistence is a well-documented intraday phenomenon. The combination—testing whether momentum predictability is regime-dependent on volume—is not yet extensively studied on 5-minute bars in index futures, making it a candidate for empirical discovery. The sources confirm that intraday bar sequences and volume forecasting both exhibit learnable structure.
