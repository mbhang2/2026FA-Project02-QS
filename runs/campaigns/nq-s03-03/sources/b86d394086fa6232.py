"""Investigate whether an outsized intra-session move tends to continue or give back.

This strategy tests the **give-back** hypothesis with a focus on **fading an
outsized bar only after a partial retracement confirms the reversion**. The prior
fade attempt (#10) entered immediately at the next bar's open, which may have
been too early if the initial move still had momentum. Here we wait for the price
to retrace a meaningful fraction of the outsized bar's range (default 0.5) before
entering in the opposite direction.

The mechanism is claimed to operate throughout the session: temporary imbalances
should revert regardless of hour, month, or weekday.

Key design choices:
- Rolling ATR (default 20 bars) adapts to current volatility.
- Outsized bar: range >= range_mult * ATR (default 2.0).
- Retracement confirmation: for an up move, the current bar's low must fall below
  the outsized bar's close minus retrace_frac * outsized_range; for a down move,
  the current bar's high must rise above the outsized bar's close plus
  retrace_frac * outsized_range.
- Entry is on the bar that confirms the retracement, in the opposite direction.
- Bracket exits use ATR-scaled offsets, with target larger than stop to allow
  the reversion to run.
"""

from __future__ import annotations


class Strategy:
    """Fade outsized moves after a retracement confirms reversion."""

    def __init__(self, *, interval: str = "5m", atr_window: int = 20,
                 range_mult: float = 2.0, retrace_frac: float = 0.5,
                 stop_atr: float = 1.0, target_atr: float = 2.0,
                 size: int = 1):
        self.interval = interval
        self.atr_window = atr_window
        self.range_mult = range_mult
        self.retrace_frac = retrace_frac
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.size = size

        # Per-session state.
        self._session = None
        self._trs: list[float] = []
        self._prev_close = None
        self._outsized_bar = None
        self._retrace_level = None

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
            self._retrace_level = None

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

        # If we have an outsized bar pending, check for retracement.
        if self._outsized_bar is not None:
            outsized_bar = self._outsized_bar
            direction = 1 if outsized_bar.close > outsized_bar.open else -1
            outsized_range = outsized_bar.high - outsized_bar.low

            if direction > 0:
                # Outsized up move: look for a down retracement.
                # Retracement level = outsized close - retrace_frac * range.
                retrace_level = outsized_bar.close - self.retrace_frac * outsized_range
                if bar.low <= retrace_level:
                    # Confirmed retracement: fade the up move with a short.
                    ctx.sell(
                        size=self.size,
                        stop_offset=self.stop_atr * atr,
                        target_offset=self.target_atr * atr,
                        tag="fade-retrace-short",
                    )
                    self._outsized_bar = None
                    self._retrace_level = None
            else:
                # Outsized down move: look for an up retracement.
                retrace_level = outsized_bar.close + self.retrace_frac * outsized_range
                if bar.high >= retrace_level:
                    # Confirmed retracement: fade the down move with a long.
                    ctx.buy(
                        size=self.size,
                        stop_offset=self.stop_atr * atr,
                        target_offset=self.target_atr * atr,
                        tag="fade-retrace-long",
                    )
                    self._outsized_bar = None
                    self._retrace_level = None

        # Identify new outsized bars.
        bar_range = bar.high - bar.low
        if bar_range >= self.range_mult * atr:
            self._outsized_bar = bar

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
