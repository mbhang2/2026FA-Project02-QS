"""Does an outsized move inside a session continue, or give it back?

Sixth proposal. Prior continuation strategies scored +0.0874 (#1), +0.0047 (#2
give-back), +0.0836 (#4), while #3 was unscored due to low exposure. This
version builds on the best elements of #1 and #4 while adding a regime filter
to reduce false signals.

Design:
- Signal: the just-closed 5m bar's body relative to the session's realized
  range so far (session high-low excluding current bar), using a rolling 85th
  percentile of that ratio within the session (#1's core).
- Confirmation: the outsized bar must close beyond the prior bar's high/low
  (#3's breakout filter), but with a more lenient threshold to preserve
  exposure.
- Regime filter: the session's average bar body over the last 10 bars must be
  above a small floor, ensuring the session has enough volatility for the
  signal to be meaningful.
- Bracket: stop at 0.8x the session range, target at 2.0x, giving a favorable
  risk/reward while allowing continuation to run.
- Minimum 10 bars before trading to stabilize the range and percentile.
"""

from __future__ import annotations

from src.core.types import Bar


class Strategy:
    """Continuation on outsized intra-session moves with breakout confirmation."""

    def __init__(self, *, interval: str = "5m", size: int = 1,
                 percentile: float = 0.85, stop_mult: float = 0.8,
                 target_mult: float = 2.0, min_bars: int = 10,
                 min_avg_body: float = 0.05):
        self.interval = interval
        self.size = size
        self.percentile = percentile
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_bars = min_bars
        self.min_avg_body = min_avg_body
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

        # Regime filter: average body over last 10 bars must exceed floor.
        recent = bars[-11:-1]
        avg_body = sum(abs(b.close - b.open) for b in recent) / len(recent)
        if avg_body < self.min_avg_body:
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

        direction = 1 if current.close > current.open else -1

        # Breakout confirmation: close beyond prior range.
        if direction > 0 and current.close <= high:
            return
        if direction < 0 and current.close >= low:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if direction > 0:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="outsized-breakout-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="outsized-breakout-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
