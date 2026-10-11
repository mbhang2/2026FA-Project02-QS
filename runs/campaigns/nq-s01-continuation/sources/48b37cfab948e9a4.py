"""Does an outsized move inside a session continue, or give it back?

Fourth proposal. The prior continuation strategy scored +0.0874, but the
give-back inversion scored only +0.0047. This version returns to the
continuation hypothesis with several refinements aimed at improving trade
selectivity and risk-adjusted returns:

- The signal is computed on the just-closed 5m bar's body relative to the
  session's realized range so far, as before.
- Instead of a single rolling percentile threshold, we require the ratio to
  exceed both a rolling 80th percentile AND a fixed minimum (0.35). This
  filters out marginal "outsized" bars that are merely noise relative to a
  tight range.
- We add a confirmation filter: the current bar's close must be beyond the
  prior bar's high (for longs) or below the prior bar's low (for shorts).
  This ensures the outsized move actually broke out of the recent range,
  rather than being a large-bodied bar that stayed inside it.
- The bracket is asymmetric: stop at 0.75x the session range, target at 2.5x.
  This gives the trade room to breathe while targeting a larger continuation.
- Minimum session bars before trading is raised to 15, so the range and
  percentile are stable.
"""

from __future__ import annotations

from src.core.types import Bar


class Strategy:
    """Continuation on outsized intra-session moves with confirmation."""

    def __init__(self, *, interval: str = "5m", size: int = 1,
                 percentile: float = 0.80, min_ratio: float = 0.35,
                 stop_mult: float = 0.75, target_mult: float = 2.5,
                 min_bars: int = 15):
        self.interval = interval
        self.size = size
        self.percentile = percentile
        self.min_ratio = min_ratio
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

        if ratio < threshold or ratio < self.min_ratio:
            return

        direction = 1 if current.close > current.open else -1

        # Confirmation: the outsized bar must break the prior range.
        if direction > 0 and current.close <= high:
            return
        if direction < 0 and current.close >= low:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if direction > 0:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="outsized-confirmed-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="outsized-confirmed-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
