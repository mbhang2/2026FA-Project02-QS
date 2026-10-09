# intraday-momentum-persistence-lstm-gradient-boost

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Five-minute OHLCV sequences in equity index futures exhibit directional autocorrelation that machine learning models can extract via gradient boosting or LSTM architectures, allowing next-bar direction prediction above random guessing.

## The claim this makes testable

If five-minute momentum persists within rolling windows of MNQ futures, then LSTM or gradient boosting models trained on expanding windows of historical 5m bars will produce directional predictions with win-rate materially above 50% on forward-looking bars.

## How to make it concrete

Extract OHLCV features from 5m bars (open, high, low, close, volume, returns, volatility, momentum indicators); construct expanding-window rolling train/test splits; train LSTM and gradient boosting models on sequence windows; measure directional accuracy on subsequent unseen bars; compare against baseline random classifier.

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: MNQ or ES 5-minute OHLCV data spanning multiple years; expanding-window walk-forward framework with rolling retraining

## Where it came from

- [Sequential Structure in Intraday Futures Data: LSTM vs Gradient Boosting on MNQ](http://arxiv.org/abs/2605.17724v1): Five-minute OHLCV bar sequences in MNQ futures contain exploitable sequential predictive structure measurable through LSTM and gradient boosting architectures under expanding-window evaluation.

## Why it is worth a trial

Recent foundation-model research on candlestick sequences suggests intraday bars encode directional information; the cited paper directly tests LSTM and gradient boosting on MNQ 5m data, providing methodological precedent for this mechanism at the micro-contract level.
