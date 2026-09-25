# ma-deviation-mean-reversion-5m

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Price deviations from short-term moving averages exhibit mean-reversion within a single trading session due to order flow imbalances and information asymmetry creating temporary mispricings that are corrected as new information arrives.

## The claim this makes testable

If mean-reversion exists in intraday deviations from moving averages, then the magnitude of price displacement from a short-term moving average will negatively correlate with subsequent price movement direction over the next 5-minute bar.

## How to make it concrete

Compute deviation of close price from 9-bar and 20-bar simple moving averages at each 5-minute bar. Measure subsequent bar returns conditional on deviation magnitude and sign. Stratify by session time and volatility regime. Test whether negative deviations predict positive next-bar returns and positive deviations predict negative next-bar returns with consistent directionality.

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: Single-session intraday data required; sufficient volume to compute reliable moving averages within session

## Where it came from

- [The Price of BitCoin: GARCH Evidence from High Frequency Data](http://arxiv.org/abs/1812.09452v1): Price formation responds to transaction demand and speculative demand dynamics that create temporary deviations from efficient pricing, measurable through conditional heteroscedasticity frameworks.
- [Understanding the Impacts of Dark Pools on Price Discovery](http://arxiv.org/abs/1612.08486v1): Information asymmetry and heterogeneous trader signals create sorting effects where different trader types execute at different venues, inducing temporary price dislocations relative to aggregate information.

## Why it is worth a trial

Mean-reversion in short-horizon price deviations is a foundational principle in market microstructure theory. The mechanism is grounded in the economic reality that temporary liquidity imbalances and order-flow-induced pricing must eventually revert as information diffuses. Testing this on 5-minute bars within single sessions isolates intraday dynamics from longer-term trends and overnight gaps, providing a clean laboratory for the hypothesis.
