"""Does an outsized move inside a session continue, or give it back?

Third proposal. The previous continuation strategy scored +0.0874 with 209
trades. This version shifts the hypothesis to **give-back**: an outsized
intra-session move is a temporary imbalance, and price drifts back toward its
recent mean. We enter *against* the outsized move, expecting reversion.

Key changes from #1:
- Signal direction is inverted: an outsized up bar triggers a short, an
  outsized down bar triggers a long.
- The threshold uses a lower percentile (0.75) so we act on less extreme moves,
  which are more likely to revert.
- Bracket is tighter on the stop (0.5x range) and wider on the target (2.0x
  range), reflecting the expectation of a smaller adverse excursion before
  reversion.
- A minimum lookback of 10 bars is required before the first trade, so the
  session range is meaningful.
"""

from __future__ import annotations

from src.core.types import Bar


class Strategy:
    """Give-back on outsized intra-session moves, window-safe."""

    def __init__(self, *, interval: str = "5m", size: int = 1,
                 percentile: float = 0.75, stop_mult: float = 0.5,
                 target_mult: float = 2.0, min_bars: int = 10):
        self.interval = interval
        self.size = size
        self.percentile = percentile
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_bars = min_bars
        self._session = None
        self._session_bars: list[Bar] = []
        self._session_ratios: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_bars = []
            self._session_ratios = []

        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        last = history[-1]
        if last.session != ctx.session:
            return
        self._session_bars.append(last)

        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        bars = self._session_bars
        if len(bars) < self.min_bars:
            return

        current = bars[-1]
        if current.close == current.open:
            return

        prior = bars[:-1]
        high = max(b.high for b in prior)
        low = min(b.low for b in prior)
        rng = high - low
        if rng <= 0:
            return

        move = abs(current.close - current.open)
        ratio = move / rng
        self._session_ratios.append(ratio)

        if len(self._session_ratios) < 5:
            return

        sorted_ratios = sorted(self._session_ratios)
        idx = int(self.percentile * (len(sorted_ratios) - 1))
        threshold = sorted_ratios[idx]

        if ratio < threshold:
            return

        # Give-back: enter against the outsized move.
        direction = -1 if current.close > current.open else 1
        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if direction > 0:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="giveback-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="giveback-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
