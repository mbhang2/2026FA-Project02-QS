# intraday-momentum-persistence-5m

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Early-session directional pressure in equity index futures persists into later sessions through incomplete price discovery and behavioral momentum, measurable as correlation between non-overlapping 5-minute bar sequences within a single trading day.

## The claim this makes testable

If intraday momentum persistence exists, then the sign and magnitude of returns in the first quartile of trading hours should exhibit predictive power for returns in the final quartile of trading hours when tested on 5-minute bars across an expanding window.

## How to make it concrete

Construct lagged return sequences from 5-minute OHLCV bars; compute directional agreement (sign correlation) and return autocorrelation structure across intraday horizons; measure predictive lift of early-session bar sequences for late-session bar returns using gradient boosting or LSTM on sequential features; control for volatility regime and volume clustering effects within the day.

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: 944+ trading days of continuous 5-minute OHLCV bars; index futures only (MNQ, ES, or equivalent); full trading sessions required; no gaps permitted within session.

## Where it came from

- [Sequential Structure in Intraday Futures Data: LSTM vs Gradient Boosting on MNQ](http://arxiv.org/abs/2605.17724v1): Five-minute OHLCV bar sequences in equity index futures contain exploitable sequential predictive structure measurable through machine learning architectures.
- [Does intraday time-series momentum exist in Chinese stock index futures market?](https://www.semanticscholar.org/paper/7a284b669d760fc22df58b50801e14b6fd090a80): First trading-session returns predict last trading-session returns in index futures, with stronger predictability at 60-minute aggregation levels and under elevated volume and volatility regimes.
- [Market intraday momentum](https://www.semanticscholar.org/paper/eb5931b46eb39831d053babe7bfed0f4687f0cea): Intraday momentum patterns—where early-session returns predict late-session returns—are documented in equity index futures and ETFs, with stronger effects during high-volatility and high-volume periods.

## Why it is worth a trial

Multiple independent markets (US, China, international) exhibit intraday momentum at session-level horizons; recent deep-learning research confirms sequential predictive structure in 5-minute futures bars; mechanism aligns with incomplete price discovery and behavioral persistence within a single trading session, making it a natural candidate for sub-minute-to-minute-scale research using the available 5-minute data harness.
