"""Investigate whether an outsized intra-session move tends to continue or give back.

This strategy tests the **give-back** hypothesis with a different twist than
prior attempts. Instead of fading the move immediately, we wait for a
consolidation pattern after the outsized move and then fade the move if the
consolidation resolves against the move's direction.

The mechanism is claimed to operate throughout the session: temporary imbalances
should revert regardless of hour, month, or weekday.

Key design choices:
- Outsized bar: range >= range_mult * ATR (default 2.5, slightly higher than
  prior attempts to focus on genuinely outsized moves).
- After the outsized bar, we wait for a consolidation of N bars (default 3)
  where the range of each bar is below a fraction of the outsized range.
- Fade signal: the current bar closes back inside the outsized bar's body
  (between open and close), indicating the move is giving back.
- Entry is in the opposite direction of the outsized move, with a bracket
  using ATR-scaled offsets.
"""

from __future__ import annotations

from typing import Optional, Tuple

from src.core.types import Bar


class Strategy:
    """Trade give-back after outsized move and consolidation."""

    def __init__(self, *, interval: str = "5m", atr_window: int = 20,
                 range_mult: float = 2.5, consolidation_bars: int = 3,
                 consolidation_frac: float = 0.5,
                 stop_atr: float = 1.0, target_atr: float = 2.0,
                 size: int = 1):
        self.interval = interval
        self.atr_window = atr_window
        self.range_mult = range_mult
        self.consolidation_bars = consolidation_bars
        self.consolidation_frac = consolidation_frac
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.size = size

        # Per-session state.
        self._session: Optional[int] = None
        self._trs: list[float] = []
        self._prev_close: Optional[float] = None
        self._outsized_bar: Optional[Tuple[int, Bar]] = None

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

        # Identify outsized bars.
        bar_range = bar.high - bar.low
        if bar_range >= self.range_mult * atr:
            # Record this as the latest outsized bar.
            self._outsized_bar = (len(history) - 1, bar)
            return

        # If we have an outsized bar, check for consolidation and fade.
        if self._outsized_bar is None:
            return

        outsized_idx, outsized_bar = self._outsized_bar
        bars_since = len(history) - 1 - outsized_idx

        # Need at least consolidation_bars after the outsized bar.
        if bars_since < self.consolidation_bars:
            return

        # Check that the bars since the outsized bar have been consolidating.
        consolidating = True
        for i in range(outsized_idx + 1, len(history) - 1):
            b = history[i]
            if (b.high - b.low) > self.consolidation_frac * (outsized_bar.high - outsized_bar.low):
                consolidating = False
                break
        if not consolidating:
            # The consolidation pattern is broken; clear the outsized bar.
            self._outsized_bar = None
            return

        # Determine direction of outsized move.
        direction = 1 if outsized_bar.close > outsized_bar.open else -1

        # Fade signal: current bar closes back inside the outsized bar's body.
        if direction > 0:
            # Outsized up move: fade if close falls below outsized bar's close
            # (or below its midpoint, indicating give-back).
            if bar.close < outsized_bar.close:
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_atr * atr,
                    target_offset=self.target_atr * atr,
                    tag="giveback-short",
                )
                self._outsized_bar = None
        else:
            # Outsized down move: fade if close rises above outsized bar's close.
            if bar.close > outsized_bar.close:
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_atr * atr,
                    target_offset=self.target_atr * atr,
                    tag="giveback-long",
                )
                self._outsized_bar = None

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
