"""Investigate whether an outsized intra-session move tends to continue or give back.

This strategy tests a **continuation** hypothesis with a focus on the
persistence of an outsized move over a longer horizon. Instead of acting on the
bar immediately following the outsized bar, we wait for a multi-bar
confirmation: the outsized bar must be followed by a sequence of bars that
continue in the same direction, and then we enter on a pullback within that
sequence.

The mechanism is claimed to operate throughout the session: information-driven
moves should continue regardless of hour, month, or weekday.

Key design choices:
- Rolling ATR (default 20 bars) adapts to current volatility.
- Outsized bar: range >= range_mult * ATR (default 2.0).
- Persistence window: after the outsized bar, we track the next `persistence`
  bars (default 3). If at least `min_continue` of them close in the same
  direction as the outsized bar, the move is considered persistent.
- Pullback entry: after persistence is confirmed, we wait for a bar that
  closes against the direction but stays within the outsized bar's range
  (i.e., does not fully retrace). We enter in the original direction on the
  next bar after the pullback.
- Bracket exits use ATR-scaled offsets, with target larger than stop to let
  the continuation run.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of outsized moves with persistence confirmation and pullback entry."""

    def __init__(self, *, interval: str = "5m", atr_window: int = 20,
                 range_mult: float = 2.0, persistence: int = 3,
                 min_continue: int = 2, pullback_frac: float = 0.5,
                 stop_atr: float = 1.0, target_atr: float = 2.0,
                 size: int = 1):
        self.interval = interval
        self.atr_window = atr_window
        self.range_mult = range_mult
        self.persistence = persistence
        self.min_continue = min_continue
        self.pullback_frac = pullback_frac
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.size = size

        # Per-session state.
        self._session = None
        self._trs: list[float] = []
        self._prev_close = None
        self._outsized_bar = None
        self._direction = 0
        self._bars_since_outsized = 0
        self._continue_count = 0
        self._pending_pullback = False

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
            self._direction = 0
            self._bars_since_outsized = 0
            self._continue_count = 0
            self._pending_pullback = False

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

        # Check for pullback entry if pending.
        if self._pending_pullback and self._outsized_bar is not None:
            # Pullback bar: closes against the direction but stays within
            # the outsized bar's range.
            if self._direction > 0 and bar.close < bar.open:
                # Down pullback in an up move.
                if bar.low >= self._outsized_bar.low + self.pullback_frac * (self._outsized_bar.high - self._outsized_bar.low):
                    ctx.buy(
                        size=self.size,
                        stop_offset=self.stop_atr * atr,
                        target_offset=self.target_atr * atr,
                        tag="continuation-pullback-long",
                    )
                    self._reset_signal()
            elif self._direction < 0 and bar.close > bar.open:
                # Up pullback in a down move.
                if bar.high <= self._outsized_bar.high - self.pullback_frac * (self._outsized_bar.high - self._outsized_bar.low):
                    ctx.sell(
                        size=self.size,
                        stop_offset=self.stop_atr * atr,
                        target_offset=self.target_atr * atr,
                        tag="continuation-pullback-short",
                    )
                    self._reset_signal()
            return

        # Track persistence after an outsized bar.
        if self._outsized_bar is not None:
            self._bars_since_outsized += 1

            if self._bars_since_outsized > self.persistence:
                # Persistence window over; check if move was persistent.
                if self._continue_count >= self.min_continue:
                    self._pending_pullback = True
                else:
                    self._reset_signal()
            else:
                # Count continuation bars within the window.
                if self._direction > 0 and bar.close > bar.open:
                    self._continue_count += 1
                elif self._direction < 0 and bar.close < bar.open:
                    self._continue_count += 1

        # Identify new outsized bars.
        bar_range = bar.high - bar.low
        if bar_range >= self.range_mult * atr:
            self._outsized_bar = bar
            self._direction = 1 if bar.close > bar.open else -1
            self._bars_since_outsized = 0
            self._continue_count = 0
            self._pending_pullback = False

    def _reset_signal(self) -> None:
        self._outsized_bar = None
        self._direction = 0
        self._bars_since_outsized = 0
        self._continue_count = 0
        self._pending_pullback = False

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
