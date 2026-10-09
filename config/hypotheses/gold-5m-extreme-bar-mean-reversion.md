# gold-5m-extreme-bar-mean-reversion

Written by the researcher; the commit date is the pre-registration date.

## The mechanism, and why it might work

Spot gold exhibits transient overshoots in 5-minute bars; extreme unidirectional moves are followed by partial reversals toward the bar's open or a short-term anchor, creating a bounce opportunity.

## The claim this makes testable

If a 5-minute bar closes beyond a threshold distance from its open in one direction, the next N bars will exhibit a net return in the opposite direction that exceeds the return of a random-walk baseline.

## How to make it concrete

Identify 5-minute bars with extreme intrabar range relative to volatility; enter short positions after large up-bars and long positions after large down-bars; exit after a fixed hold period or a threshold reversal.

## The precise specification

Let C_t, O_t, H_t, L_t be close, open, high, low of bar t. Define intrabar return r_t = (C_t - O_t) / O_t. Define rolling 20-bar realized volatility vol_t = stdev(r_{t-19:t}). Define extremeness metric e_t = |r_t| / vol_t. Entry rule: if e_t > threshold_e (default 2.0, range 1.5–3.0) and r_t > 0, enter short 1 unit at open of bar t+1; if e_t > threshold_e and r_t < 0, enter long 1 unit at open of bar t+1. Exit rule: hold for hold_bars bars (default 5, range 2–10) or exit at profit_target pips (default 10, range 5–25) in the reversal direction, whichever comes first. Stop-loss at stop_loss pips (default 20, range 15–40) in the entry direction. Measure outcome as close price of exit bar minus entry price, divided by entry price, expressed in basis points.

## Data it needs

- interval: 5m
- needs ticks: no
- needs spreads: no
- other: Spot gold OHLC; no bid–ask spread or tick-level data required.

## Where it came from

- [Mean Reversion Trading: Proven Strategies for Higher Returns](https://enlightenedstocktrading.com/mean-reversion/): Mean reversion traders profit from bounces by exiting after minimal favorable moves rather than waiting for full reversals.
- [Short-horizon mean reversion in cryptocurrency markets](https://arxiv.org/pdf/2608.21888): Short-horizon mean reversion in 5-minute bars is a recognized phenomenon, though its reliability is context-dependent.
- [Exploring Mean Reversion: Techniques and Insights for Investment](https://www.investopedia.com/terms/m/meanreversion.asp): Intraday moving averages and short-term anchors help day traders identify mean prices and entry/exit signals.
- [Understanding FX Trading Opportunities in Overextended Price Moves](https://titanfx.com/education/understanding-fx-trading-opportunities-in-overextended-price-moves): Waiting for a reversal candle after a sharp move is a sign that momentum may be weakening, offering a trade opportunity.

## Why it is worth a trial

5-minute bars in spot gold are liquid and exhibit microstructure noise; extreme single-bar moves often reflect order imbalance or liquidity shocks rather than fundamental repricing. The bounce is a plausible mechanical phenomenon driven by order-book mean reversion. The mechanism is narrow in scope (single-bar extremeness) and time horizon (5–10 bars), making it testable on high-frequency data without requiring tick-level detail.
