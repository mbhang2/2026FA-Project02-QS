"""Investigate whether an outsized intra-session move tends to continue or give back.

This strategy tests the **continuation** hypothesis with a focus on **follow-through
after an outsized bar that closes near its extreme**. Instead of requiring a further
breakout beyond the outsized bar's high/low, we look for an outsized bar whose close
is in the top or bottom quartile of its range (strong directional close), and then
enter in that direction on the next bar if the next bar's close confirms the move
by closing beyond the outsized bar's close in the same direction.

The mechanism is claimed to operate throughout the session: information-driven moves
should continue regardless of hour, month, or weekday.

Key design choices:
- Rolling ATR (default 20 bars) adapts to current volatility.
- Outsized bar: range >= range_mult * ATR (default 2.0).
- Strong close: the outsized bar's close must be in the top 25% of its range for
  up moves, or bottom 25% for down moves (close_fraction=0.25).
- Confirmation: the current bar's close must exceed the outsized bar's close in
  the direction of the move (no additional breakout required).
- Bracket exits use ATR-scaled offsets.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of outsized moves with strong close confirmation."""

    def __init__(self, *, interval: str = "5m", atr_window: int = 20,
                 range_mult: float = 2.0, close_fraction: float = 0.25,
                 stop_atr: float = 1.0, target_atr: float = 2.0,
                 size: int = 1):
        self.interval = interval
        self.atr_window = atr_window
        self.range_mult = range_mult
        self.close_fraction = close_fraction
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

        # Check if the previous bar was outsized and had a strong close.
        if self._outsized_bar is not None:
            outsized_bar = self._outsized_bar
            direction = 1 if outsized_bar.close > outsized_bar.open else -1

            if direction > 0:
                # Up move: current close must exceed outsized close.
                if bar.close > outsized_bar.close:
                    ctx.buy(
                        size=self.size,
                        stop_offset=self.stop_atr * atr,
                        target_offset=self.target_atr * atr,
                        tag="continuation-strong-long",
                    )
                    self._outsized_bar = None
            else:
                # Down move: current close must fall below outsized close.
                if bar.close < outsized_bar.close:
                    ctx.sell(
                        size=self.size,
                        stop_offset=self.stop_atr * atr,
                        target_offset=self.target_atr * atr,
                        tag="continuation-strong-short",
                    )
                    self._outsized_bar = None

        # Identify new outsized bars with strong closes.
        bar_range = bar.high - bar.low
        if bar_range >= self.range_mult * atr:
            if bar_range <= 0:
                return
            close_position = (bar.close - bar.low) / bar_range
            if bar.close > bar.open and close_position >= 1.0 - self.close_fraction:
                self._outsized_bar = bar
            elif bar.close < bar.open and close_position <= self.close_fraction:
                self._outsized_bar = bar

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
