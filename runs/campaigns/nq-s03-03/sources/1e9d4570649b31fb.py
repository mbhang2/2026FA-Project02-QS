"""Investigate whether an outsized intra-session move tends to continue or give back.

This strategy tests the **give-back** hypothesis with a focus on **mean reversion
after an outsized move that has already shown some reversal**. Instead of fading
immediately, we wait for the outsized move to occur, then wait for the price to
retrace a meaningful portion of that move (indicating the give-back has begun),
and then enter in the direction of the retracement, expecting it to continue.

The mechanism is claimed to operate throughout the session: temporary imbalances
should revert regardless of hour, month, or weekday.

Key design choices:
- Outsized bar: range >= range_mult * ATR (default 2.0).
- Retracement trigger: after the outsized bar, we wait for price to retrace at
  least retracement_frac (default 0.5) of the outsized bar's range from its
  extreme in the opposite direction. This confirms the give-back is underway.
- Entry: when the current bar's close confirms the retracement, we enter in the
  direction of the retracement (opposite the outsized move).
- Bracket exits use ATR-scaled offsets.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import Bar


class Strategy:
    """Trade give-back after outsized move and meaningful retracement."""

    def __init__(self, *, interval: str = "5m", atr_window: int = 20,
                 range_mult: float = 2.0, retracement_frac: float = 0.5,
                 stop_atr: float = 1.0, target_atr: float = 2.0,
                 size: int = 1):
        self.interval = interval
        self.atr_window = atr_window
        self.range_mult = range_mult
        self.retracement_frac = retracement_frac
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.size = size

        # Per-session state.
        self._session: Optional[int] = None
        self._trs: list[float] = []
        self._prev_close: Optional[float] = None
        self._outsized_bar: Optional[Bar] = None
        self._outsized_idx: Optional[int] = None

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
            self._outsized_idx = None

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

        # Identify outsized bars.
        bar_range = bar.high - bar.low
        if bar_range >= self.range_mult * atr:
            # Record this as the latest outsized bar.
            self._outsized_bar = bar
            self._outsized_idx = len(history) - 1
            return

        # If we have an outsized bar, check for retracement.
        if self._outsized_bar is None or self._outsized_idx is None:
            return

        outsized_bar = self._outsized_bar
        outsized_idx = self._outsized_idx
        bars_since = len(history) - 1 - outsized_idx

        # Only consider retracement after at least 1 bar has passed.
        if bars_since < 1:
            return

        # Direction of outsized move: close vs open.
        direction = 1 if outsized_bar.close > outsized_bar.open else -1

        # Retracement level: for an up move, price must fall back below
        # outsized_bar.high - retracement_frac * range; for a down move, price
        # must rise above outsized_bar.low + retracement_frac * range.
        outsized_range = outsized_bar.high - outsized_bar.low
        if outsized_range <= 0:
            return

        if direction > 0:
            # Up move: retracement means close falls below the threshold.
            retracement_level = outsized_bar.high - self.retracement_frac * outsized_range
            if bar.close < retracement_level:
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_atr * atr,
                    target_offset=self.target_atr * atr,
                    tag="giveback-retracement-short",
                )
                self._outsized_bar = None
                self._outsized_idx = None
        else:
            # Down move: retracement means close rises above the threshold.
            retracement_level = outsized_bar.low + self.retracement_frac * outsized_range
            if bar.close > retracement_level:
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_atr * atr,
                    target_offset=self.target_atr * atr,
                    tag="giveback-retracement-long",
                )
                self._outsized_bar = None
                self._outsized_idx = None

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
