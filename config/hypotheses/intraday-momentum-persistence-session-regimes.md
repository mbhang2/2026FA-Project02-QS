# intraday-momentum-persistence-session-regimes

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Momentum in 5-minute returns of equity index futures persists within intraday session regimes (e.g., open, mid-day, close) due to order-flow clustering and mean-reversion friction that varies by session liquidity and participation patterns.

## The claim this makes testable

If momentum persistence is regime-dependent, then lagged 5-minute returns will have higher predictive power for forward returns when conditioned on session regime membership than when treated as a pooled time series.

## How to make it concrete

Construct momentum features from 5-minute bar closes (e.g., 1-bar, 2-bar, 5-bar lagged returns); partition bars by session regime (pre-market, regular hours open, mid-session, final hour, close); regress forward 5-minute returns on momentum features separately within each regime; compare feature coefficients and their consistency across regimes.

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: Intraday timestamps to define session boundaries; multi-year 5-minute OHLCV bar sequences for equity index futures (ES, NQ, or MNQ)

## Where it came from

- [Sequential Structure in Intraday Futures Data: LSTM vs Gradient Boosting on MNQ](http://arxiv.org/abs/2605.17724v1): Five-minute OHLCV bar sequences in single-instrument intraday futures data contain exploitable sequential structure measurable across extended walk-forward periods.

## Why it is worth a trial

Intraday session regimes reflect structural shifts in participant composition, order intensity, and inventory constraints. Prior work demonstrates that 5-minute bar sequences in equity index futures exhibit predictive structure; decomposing that structure by session regime isolates whether momentum persistence is a universal property or emerges conditionally from regime-specific microstructure.
