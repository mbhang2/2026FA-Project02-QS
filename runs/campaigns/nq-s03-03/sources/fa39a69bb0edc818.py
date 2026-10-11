"""Investigate whether an outsized intra-session move tends to continue or give back.

This strategy tests the give-back hypothesis with a refined entry that requires
both an outsized range bar and a subsequent reversal bar, but now with a
stricter definition of outsized: the range must exceed a multiple of the
session's recent ATR (measured over a rolling window rather than cumulative
average), and the reversal must be meaningful relative to the outsized bar's
range. The rolling ATR adapts more quickly to changing volatility, making the
outsized threshold responsive to current conditions rather than diluted by the
session's early quiet bars.

The mechanism is claimed to operate throughout the session, not just in specific
hours, because the temporary liquidity imbalance that creates an outsized move
can occur at any time. The strategy does not condition on month, weekday, or
hour.

Key improvements over prior attempts:
- Rolling ATR window (default 20 bars) instead of cumulative average, so the
  outsized threshold reflects recent volatility.
- The reversal bar must close at least a fraction of the outsized bar's range
  back in the opposite direction, not merely have a down close after an up bar.
- A minimum reversal fraction filters out weak confirmations that are likely
  noise.
"""

from __future__ import annotations


class Strategy:
    """Trade give-back of outsized moves with rolling ATR and stronger confirmation."""

    def __init__(self, *, interval: str = "5m", atr_window: int = 20,
                 range_mult: float = 2.0, reversal_frac: float = 0.3,
                 stop_atr: float = 1.0, target_atr: float = 2.0,
                 size: int = 1):
        self.interval = interval
        self.atr_window = atr_window
        self.range_mult = range_mult
        self.reversal_frac = reversal_frac
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.size = size

        # Per-session state.
        self._session = None
        self._trs: list[float] = []
        self._prev_close = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset state when the session changes.
        if ctx.session != self._session:
            self._session = ctx.session
            self._trs = []
            self._prev_close = None

        history = ctx.history(self.interval)
        if len(history) < 3:
            return

        bar = history[-1]

        # Update true range series.
        if self._prev_close is None:
            self._prev_close = bar.open
        tr = max(bar.high - bar.low,
                 abs(bar.high - self._prev_close),
                 abs(bar.low - self._prev_close))
        self._trs.append(tr)
        self._prev_close = bar.close

        # Need at least a few bars for a meaningful ATR.
        if len(self._trs) < 3:
            return

        # Rolling ATR over the most recent bars.
        window = self._trs[-self.atr_window:]
        atr = sum(window) / len(window)
        if atr <= 0:
            return

        # One position at a time; the bracket handles exits.
        if ctx.position or ctx.working_order_ids:
            return

        prev_bar = history[-2]
        prev_range = prev_bar.high - prev_bar.low
        if prev_range < self.range_mult * atr:
            return

        # Direction of the outsized move: close vs open.
        prev_direction = 1 if prev_bar.close > prev_bar.open else -1

        # Reversal must be meaningful: the current bar's close must be at least
        # reversal_frac of the outsized bar's range back in the opposite
        # direction from the outsized bar's close.
        if prev_direction > 0:
            # Outsized up move: reversal means close below prev_bar.close by
            # at least reversal_frac * prev_range.
            required_drop = self.reversal_frac * prev_range
            if bar.close > prev_bar.close - required_drop:
                return
        else:
            # Outsized down move: reversal means close above prev_bar.close by
            # at least reversal_frac * prev_range.
            required_rise = self.reversal_frac * prev_range
            if bar.close < prev_bar.close + required_rise:
                return

        # Confirmed give-back: trade against the outsized move.
        if prev_direction > 0:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_atr * atr,
                target_offset=self.target_atr * atr,
                tag="giveback-short",
            )
        else:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_atr * atr,
                target_offset=self.target_atr * atr,
                tag="giveback-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
