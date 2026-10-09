# intraday-momentum-persistence-lstm-sequential

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Five-minute OHLCV bar sequences in equity index futures contain exploitable directional predictive structure that LSTM architectures can learn to forecast near-term price momentum within rolling short-horizon windows.

## The claim this makes testable

If LSTM models trained on expanding windows of 5-minute bars achieve directional accuracy on held-forward periods that exceeds a reasonable baseline (e.g., 50% random guess or a simple lagged-return predictor), then sequential structure exists in intraday futures data.

## How to make it concrete

Extract features from rolling 5-minute OHLCV sequences: prior bar close-to-close returns, intra-bar volatility (high-low range), volume profile, and momentum indicators (e.g., rate of change over prior N bars). Train LSTM to predict next-bar direction (up/down close). Evaluate directional accuracy and information coefficient on forward-walk windows.

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: Equity index futures OHLCV data (e.g., MNQ, ES) spanning multiple years of trading days; expanding-window rolling evaluation framework

## Where it came from

- [Sequential Structure in Intraday Futures Data: LSTM vs Gradient Boosting on MNQ](http://arxiv.org/abs/2605.17724v1): LSTM and gradient boosting architectures tested on five-minute OHLCV bar sequences of MNQ across 944 trading days show that sequential structure exists in intraday futures candlestick data.

## Why it is worth a trial

Recent foundation-model research demonstrates that candlestick sequences encode learnable directive patterns. A single-instrument, high-frequency dataset (5-minute bars) offers sufficient granularity and volume to detect momentum persistence if it exists, and LSTM's ability to capture temporal dependencies makes it a natural tool to test whether short-term price sequences are predictable.
