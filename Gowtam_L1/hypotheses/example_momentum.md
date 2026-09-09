# Time-series momentum (Jegadeesh & Titman, 1993)

Buying recent winners and selling recent losers, adapted to a single asset in
long/flat form.

## Signal

- Compute a 20-bar simple moving average of `close` (SMA20).
- Compute the trailing 20-bar return: `close / close.shift(20) - 1`.

## Rule

- Go long (+1.0) when the current close is *above* SMA20 AND the 20-bar return
  is positive.
- Otherwise hold 0.0 (flat).

## Constraints

- Strictly no lookahead: only past bars may be used to decide the current bar.
- Returned positions must be index-aligned to the incoming `price_history`.
- Implement as a single `Strategy`-conforming class with `generate_positions`.
