# intraday-momentum-acceleration-persistence

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Price acceleration measured as the second derivative of returns (change in momentum) within a single session persists over short intervals due to incomplete market repricing of order flow imbalances and inventory constraints in futures markets.

## The claim this makes testable

If acceleration persistence exists, then lagged measures of return curvature (third central moment or fitted polynomial acceleration coefficients from prior bars) will exhibit positive correlation with forward directional returns within the same trading session.

## How to make it concrete

Extract from each 5-minute bar: (1) close-to-close returns, (2) fitted acceleration via polynomial regression on recent close prices or via finite-difference approximation of velocity change, (3) lagged acceleration terms (1–4 bars back), (4) forward returns (1–5 bars ahead). Regress forward returns on lagged acceleration controls, stratified by market regime (high/low volatility, trending/mean-reverting phases identified via rolling metrics).

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: OHLCV bars only; no tick-level or microstructure data required

## Where it came from

- [Sequential Structure in Intraday Futures Data: LSTM vs Gradient Boosting on MNQ](https://arxiv.org/abs/2605.17724v1): Intraday futures data exhibits sequential structure exploitable through machine learning on candlestick sequences at single-instrument scale.

## Why it is worth a trial

Momentum acceleration is a natural third-order feature of price dynamics; HJM-type frameworks model intraday price evolution through stochastic processes with economic structure, suggesting acceleration may carry information. The cited work confirms exploitable sequential patterns in 5-minute MNQ bars, motivating investigation of derivative-order features like acceleration persistence within sessions.
