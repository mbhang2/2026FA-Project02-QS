# intraday-momentum-volatility-regime

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Intraday momentum in equity index futures exhibits persistence that varies predictably across realized volatility regimes, with mean-reversion dynamics strengthening in high-volatility states due to stochastic volatility mean-reversion processes.

## The claim this makes testable

If realized volatility over a 5-minute lookback window exceeds a regime threshold, then the autocorrelation of subsequent 5-minute returns will be lower (more mean-reverting) than in low-volatility regimes, measured across rolling windows.

## How to make it concrete

Partition 5-minute returns into volatility bins using realized volatility computed from intrabar squared returns; measure lag-1 and lag-2 autocorrelation of returns within each regime; test whether autocorrelation magnitude and sign differ systematically across regimes; control for time-of-day effects and volume clustering.

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: Realized volatility computed from close-to-close 5-minute returns; regime assignment based on rolling window percentiles of realized volatility

## Where it came from

- [Mean-Reverting SABR Models: Closed-form Surfaces and Calibration for Equities](http://arxiv.org/abs/2502.07766v5): Mean-reverting stochastic volatility processes (CIR, lognormal) generate distinct dynamics of instantaneous volatility that affect asset price behavior conditionally.
- [Volatility-based strategy on Chinese equity index ETF options](http://arxiv.org/abs/2403.00474v2): Dynamic adjustment of trading strategy positions based on volatility regimes identified through GARCH models can enhance returns in volatile market conditions.

## Why it is worth a trial

Stochastic volatility mean-reversion is a well-documented property of equity index dynamics; conditional momentum behavior across volatility regimes remains under-explored at intraday frequencies where microstructure and regime-switching interact; 5-minute bars provide sufficient granularity to detect autocorrelation structure while avoiding tick-level noise.
