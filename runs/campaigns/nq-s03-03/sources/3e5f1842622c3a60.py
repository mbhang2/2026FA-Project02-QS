"""Investigate whether an outsized intra-session move tends to continue or give back.

This strategy tests the **give-back** hypothesis with a focus on **fading an
outsized bar immediately at the open of the next bar**, without waiting for a
retracement to confirm. The prior give-back attempt (#8) waited for a 50%
retracement before entering, which may have missed the initial reversal. Here we
enter at the next bar's open (approximated by the previous close) in the opposite
direction of the outsized move, with a tight ATR-scaled stop and target.

The mechanism is claimed to operate throughout the session: temporary imbalances
should revert regardless of hour, month, or weekday.

Key design choices:
- Rolling ATR (default 20 bars) adapts to current volatility.
- Outsized bar: range >= range_mult * ATR (default 2.0).
- Entry: immediately on the next bar after the outsized bar, in the opposite
  direction of the outsized move.
- Bracket exits use ATR-scaled offsets, with target larger than stop to allow
  the reversion to run.
"""

from __future__ import annotations


class Strategy:
    """Fade outsized moves immediately with ATR-scaled brackets."""

    def __init__(self, *, interval: str = "5m", atr_window: int = 20,
                 range_mult: float = 2.0, stop_atr: float = 1.0,
                 target_atr: float = 2.5, size: int = 1):
        self.interval = interval
        self.atr_window = atr_window
        self.range_mult = range_mult
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.size = size

        # Per-session state.
        self._session = None
        self._trs: list[float] = []
        self._prev_close = None
        self._outsized_bar = None

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
            self._outsized_bar = None

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

        if len(self._trs) < 3:
            return

        window = self._trs[-self.atr_window:]
        atr = sum(window) / len(window)
        if atr <= 0:
            return

        # One position at a time.
        if ctx.position or ctx.working_order_ids:
            return

        # If we have an outsized bar pending, fade it now.
        if self._outsized_bar is not None:
            outsized_bar = self._outsized_bar
            direction = 1 if outsized_bar.close > outsized_bar.open else -1

            if direction > 0:
                # Outsized up move: fade with a short.
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_atr * atr,
                    target_offset=self.target_atr * atr,
                    tag="fade-outsized-short",
                )
            else:
                # Outsized down move: fade with a long.
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_atr * atr,
                    target_offset=self.target_atr * atr,
                    tag="fade-outsized-long",
                )
            self._outsized_bar = None
            return

        # Check if the current bar is outsized; if so, prepare to fade next bar.
        bar_range = bar.high - bar.low
        if bar_range >= self.range_mult * atr:
            self._outsized_bar = bar

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
