# precious-metals-extreme-bar-mean-reversion

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Extreme single-bar returns in precious metals spot markets trigger transient liquidity dislocations that reverse within the next few bars as order flow normalizes and the EFP spread relaxes back toward equilibrium.

## The claim this makes testable

If a 5-minute bar closes with a return magnitude exceeding the rolling 20-bar volatility percentile threshold, the subsequent 1–3 bars will exhibit a mean-reverting drift opposite to the extreme bar's direction, with cumulative return magnitude smaller than the initiating bar.

## How to make it concrete

Identify 5-minute bars with extreme returns; measure mean-reversion magnitude and duration in the following bars; compare reversal strength to a baseline of non-extreme bar sequences.

## The precise specification

Let C_t denote the close of bar t. Define r_t = (C_t - C_{t-1}) / C_{t-1}. Compute rolling volatility: vol_t = std(r_{t-20:t-1}). Define extreme_threshold_pct = 90th percentile of |r_{t-20:t}| / vol_t over a training window. Entry condition: |r_t| > extreme_threshold_pct * vol_t AND |r_t| > baseline_vol_multiplier * vol_t, where baseline_vol_multiplier in [1.5, 3.0]. Exit condition: time-based at bar t+k where k in [1, 3], or price-based if cumulative return r_{t+1:t+k} reverses sign. Measure reversal strength as mean(sign(r_t) * r_{t+1:t+k}) and compare to mean(sign(r_t) * r_{t+1:t+k}) for non-extreme sequences. Parameters: rolling window = 20 bars; extreme_threshold_pct in [75, 95]; baseline_vol_multiplier in [1.5, 3.0]; hold duration k in [1, 3] bars.

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: Spot precious metals (gold, silver) OHLCV data; no futures or EFP spread data required for initial backtest, though [0] suggests EFP dynamics may confound results in live markets.

## Where it came from

- [Market Making in Spot Precious Metals](http://arxiv.org/abs/2404.15478v5): The EFP spread in precious metals exhibits multiple relaxation modes corresponding to different trading horizons, modeled as nested mean-reverting processes.
- [Model for Non-Gaussian Intraday Stock Returns](http://arxiv.org/abs/0906.3841v2): Intraday stock returns exhibit non-Gaussian dynamics with stable distributional shape over intraday time scales, driven by time-varying volatility.
- [Improving MF-DFA model with applications in precious metals market](http://arxiv.org/abs/2006.15214v1): Precious metals spot markets exhibit multifractal price fluctuations and complex scaling properties that vary across market regimes.
- [Pairtrading — Indicadores y estrategias - TradingView](https://es.tradingview.com/scripts/pairtrading/): Five-minute and three-minute timeframes are recommended for pair-trading strategies in precious metals, including relative movement analysis between gold and silver.
- [FLOW — المؤشرات والاستراتيجيات — TradingView](https://ar.tradingview.com/scripts/flow/): Intraday price behavior exhibits mean-reversion tendencies when price moves away from a point of control but returns due to market balance dynamics.

## Why it is worth a trial

Precious metals spot markets are known to exhibit non-Gaussian intraday dynamics with time-varying volatility [1][2]. The EFP spread mechanism [0] suggests that extreme bars may reflect temporary dislocations in the futures–spot relationship that relax over short horizons. Pair-trading literature [4] and market microstructure theory both suggest mean-reversion is a dominant intraday behavior in these assets. The 5-minute interval is a natural choice for capturing transient liquidity effects while avoiding noise at sub-minute scales [4]. This hypothesis is concrete, testable on spot data alone, and grounded in both theoretical and empirical precedent in precious metals markets.
