"""Investigate whether an outsized intra-session move tends to continue or give back.

This strategy tests a **give-back** hypothesis with a focus on fading an
outsized bar, but with a crucial addition: we require the outsized bar to be
followed by a bar that closes in the opposite direction of the outsized move
**and** that opposite bar's range must be at least a fraction of the outsized
bar's range. This filters for genuine reversion rather than mere drift.

The mechanism is claimed to operate throughout the session: temporary imbalances
should revert regardless of hour, month, or weekday.

Key design choices:
- Rolling ATR (default 20 bars) adapts to current volatility.
- Outsized bar: range >= range_mult * ATR (default 2.0).
- Reversion confirmation: the current bar must close in the opposite direction
  of the outsized bar, and its range must be at least reversion_frac * outsized
  range (default 0.5). This ensures the reversion is meaningful.
- Entry is on the bar that confirms the reversion, in the opposite direction.
- Bracket exits use ATR-scaled offsets, with target larger than stop to allow
  the reversion to run.
"""

from __future__ import annotations


class Strategy:
    """Fade outsized moves after a meaningful opposite bar confirms reversion."""

    def __init__(self, *, interval: str = "5m", atr_window: int = 20,
                 range_mult: float = 2.0, reversion_frac: float = 0.5,
                 stop_atr: float = 1.0, target_atr: float = 2.0,
                 size: int = 1):
        self.interval = interval
        self.atr_window = atr_window
        self.range_mult = range_mult
        self.reversion_frac = reversion_frac
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

        # Check for reversion if we have a pending outsized bar.
        if self._outsized_bar is not None:
            outsized_bar = self._outsized_bar
            direction = 1 if outsized_bar.close > outsized_bar.open else -1
            outsized_range = outsized_bar.high - outsized_bar.low

            # Current bar's direction.
            current_direction = 1 if bar.close > bar.open else -1

            # Reversion requires opposite direction and meaningful range.
            if current_direction != direction:
                current_range = bar.high - bar.low
                if current_range >= self.reversion_frac * outsized_range:
                    # Confirmed reversion: fade the outsized move.
                    if direction > 0:
                        # Outsized up move: fade with a short.
                        ctx.sell(
                            size=self.size,
                            stop_offset=self.stop_atr * atr,
                            target_offset=self.target_atr * atr,
                            tag="fade-reversion-short",
                        )
                    else:
                        # Outsized down move: fade with a long.
                        ctx.buy(
                            size=self.size,
                            stop_offset=self.stop_atr * atr,
                            target_offset=self.target_atr * atr,
                            tag="fade-reversion-long",
                        )
                    self._outsized_bar = None

        # Identify new outsized bars.
        bar_range = bar.high - bar.low
        if bar_range >= self.range_mult * atr:
            self._outsized_bar = bar

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
