# Mean reversion (Bollinger-style z-score)

Price tends to revert to a rolling mean after short-term deviations.

## Signal

- Compute a 20-bar simple moving average of `close` (SMA20) and a 20-bar rolling
  standard deviation of `close` (STD20).
- Define the z-score: `(close - SMA20) / STD20`.

## Rule

- Go long (+1.0) when the z-score is below -1.0 (oversold).
- Go flat (0.0) when the z-score is above 0.0 (reverted to the mean).
- Never go short.

## Constraints

- Strictly no lookahead.
- Positions index-aligned to the incoming `price_history`.
- Single `Strategy`-conforming class with `generate_positions`.
