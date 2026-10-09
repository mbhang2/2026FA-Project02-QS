# intraday-momentum-persistence-rolling-window

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Five-minute returns exhibit autocorrelation within rolling windows such that the sign and magnitude of recent momentum predict the direction of the next bar, with persistence strength varying across different window lengths.

## The claim this makes testable

If intraday momentum persistence exists within rolling windows, then a trading rule that enters long when cumulative returns over a rolling lookback window are positive will produce directional accuracy above 50% on held-out periods.

## How to make it concrete

Compute rolling cumulative returns over variable window lengths; enter long/short based on sign; measure directional accuracy (proportion of correct direction predictions) on forward bars.

## The precise specification

Let C_t denote the close of bar t. Define the simple return as r_t = (C_t - C_{t-1}) / C_{t-1}. For a rolling window of length W bars, compute the cumulative return as R_t^W = sum of r_{t-W+1} to r_t. Define the momentum signal as M_t^W = sign(R_t^W), where sign(x) = 1 if x > 0, -1 if x < 0, 0 if x = 0. Entry rule: at bar t, if M_t^W != 0, enter a position in direction M_t^W at the open of bar t+1. Exit rule: close the position at the close of bar t+1 (one-bar holding period). Target variable: Y_t = sign(r_{t+1}). Directional accuracy = (count of cases where M_t^W = Y_t) / (total non-zero signals). Evaluate accuracy across an expanding-window walk-forward framework: train period grows; test period is the subsequent fixed number of bars. Parameters: W in {5, 10, 15, 20, 30, 60} bars (default W=10); minimum signal frequency threshold = 10 signals per test window to avoid sparse regimes; accuracy threshold for 'persistence' = 51% (marginally above random). Repeat across multiple non-overlapping test windows within the dataset to measure consistency of the effect.

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: OHLCV bars for equity index futures (e.g., MNQ, ES) spanning at least 200+ trading days to permit expanding-window walk-forward evaluation.

## Where it came from

- [Sequential Structure in Intraday Futures Data: LSTM vs Gradient Boosting on MNQ](http://arxiv.org/abs/2605.17724v1): Five-minute OHLCV bar sequences in index futures contain exploitable sequential structure measurable via machine learning, suggesting that momentum patterns persist within intraday windows.

## Why it is worth a trial

Intraday momentum is a well-documented phenomenon in equity markets; five-minute bars are a natural granularity for observing short-term autocorrelation without requiring tick-level noise filtering. The cited source demonstrates that MNQ bar sequences yield predictive signal, motivating a direct test of whether simple rolling-window momentum rules capture this persistence. This hypothesis is falsifiable, requires only 5m OHLCV data, and can be evaluated in an expanding-window framework to avoid look-ahead bias.
