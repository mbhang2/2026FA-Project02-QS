"""Does an outsized move inside a session continue, or give it back?

Second proposal, fixed for mypy. The first crashed because it iterated over
the entire history view, which exceeds the bounded window. This version keeps
only the current session's bars on self, so it never reaches past the window.

The signal is the same continuation idea: measure the just-closed 5m bar's
close-to-close move against the session's realized range so far, using a
rolling 90th percentile of that ratio within the session. Enter in the
direction of an outsized move with a bracket sized off the session range.

The mypy errors came from typing the session bar list as `list[object]`.
The bars are `Bar` instances, so we import the type and annotate properly.
"""

from __future__ import annotations

from src.core.types import Bar


class Strategy:
    """Continuation on outsized intra-session moves, window-safe."""

    def __init__(self, *, interval: str = "5m", size: int = 1,
                 percentile: float = 0.90, stop_mult: float = 1.0,
                 target_mult: float = 1.5):
        self.interval = interval
        self.size = size
        self.percentile = percentile
        self.stop_mult = stop_mult
        self.target_mult = target_mult
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

        # Append the just-closed bar for this session.
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
        if len(bars) < 2:
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

        direction = 1 if current.close > current.open else -1
        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if direction > 0:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="outsized-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="outsized-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
