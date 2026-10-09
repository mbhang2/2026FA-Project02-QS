# intraday-momentum-latent-state-persistence

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

A hidden momentum state governs intraday directional persistence in equity index futures; when the latent state remains stable across consecutive 5-minute bars, forward returns exhibit autocorrelation that can be captured without lag-induced sign reversals.

## The claim this makes testable

If a Hidden Markov Model trained on rolling 5-minute OHLCV sequences identifies a latent momentum state with high persistence probability, then conditioning entry signals on state stability (rather than filtered returns) will reduce whipsaw trades during trend reversals within the same session.

## How to make it concrete

Extract 5-minute OHLCV features (close-to-close returns, intrabar range, volume profile); estimate a 2–3 state HMM on rolling windows; compute state transition probabilities and state duration; measure forward 5-minute return sign alignment conditional on state persistence vs. state transition; quantify reduction in directional reversals and false-signal trades.

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: OHLCV bars only; no tick-level order flow required

## Where it came from

- [Hidden Markov Models Applied To Intraday Momentum Trading With Side Information](http://arxiv.org/abs/2006.08307v1): A Hidden Markov Model specifying a latent momentum state avoids time-lagging artifacts that cause momentum signals of the wrong sign during trend reversals.
- [Sequential Structure in Intraday Futures Data: LSTM vs Gradient Boosting on MNQ](http://arxiv.org/abs/2605.17724v1): Five-minute OHLCV bar sequences in equity index futures contain exploitable sequential predictive structure at single-instrument scale.
- [Ensemble properties of high frequency data and intraday trading rules](http://arxiv.org/abs/1202.2447v2): Intraday scaling properties of aggregated return distributions can define a martingale model that replicates conditioned expectations within morning and afternoon sessions.

## Why it is worth a trial

HMM-based state inference directly addresses the lag problem in momentum detection; 5-minute bars provide sufficient frequency to capture intraday regime shifts without requiring tick data; latent state persistence is a natural null model for directional autocorrelation within a single session.
