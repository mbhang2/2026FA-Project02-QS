# Short-horizon directional reversal (Kitron & Wengrowicz, 2026)

Fading the prior bar's direction captures short-horizon mean reversion:
the signal lives in signs, not magnitudes — simply betting against the
previous candle captures most of the effect.

> Kitron, N. A. & Wengrowicz, J. M., **"Short-horizon mean reversion in
> cryptocurrency markets: a matched cross-market measurement"**,
> arXiv:2608.21888v1, 2026.

Paper findings (crypto spot, 15-min candles, matched out-of-sample protocol):
- 90% of 183 Binance pairs carry significant directional reversal, vs 2.7%
  of 187 US stocks/ETFs; present in every focal coin-year since 2021.
- Lag-one return autocorrelation is near zero on the majors, yet fading the
  prior candle captures most of the effect.
- Reversal concentrates after moves driven by aggressive taker flow and
  grows with flow intensity; the order-book depth a move consumes conditions
  nothing (consistent with compensated liquidity provision).
- Gross edge peaks near 1.3 bp/trade against ~5 bp round-trip cost: large
  enough to detect, too small to clear benchmark spot capture costs.

## Adaptation to L1 (single instrument, bar-generic)

- Original venue/horizon (multi-venue crypto spot, 15-min bars) is not
  available; test the directional claim only, on session bars of one
  futures instrument.
- Taker-flow conditioning is approximated with bar delta
  (`buy_volume - sell_volume`) and volume intensity from traded-bars data.
  Depth-consumption conditioning is untestable (no book data) and omitted.
- Cost expectation pre-registered: the paper finds gross edge below spot
  capture costs, so the success criterion is directional existence
  (out-of-sample hit rate / AUC above 0.5), not net profitability.

## Signal

- Prior-bar direction: `sign(close.shift(1) / close.shift(2) - 1)` — uses
  only closed bars, no lookahead.
- Flow-intensity gate: trailing-20-bar median of `abs(delta)`; trade only
  when the prior bar's intensity is above median.

## Rule

- Go short (-1.0) when the prior bar was up AND the intensity gate passes.
- Go long (+1.0) when the prior bar was down AND the intensity gate passes.
- Otherwise hold 0.0 (flat).

## Constraints

- Strictly no lookahead: only past bars may be used to decide the current bar.
- Returned positions must be index-aligned with incoming `price_history`.
- Implement a single `Strategy`-conforming class with `generate_positions`.
