"""Investigate whether an outsized intra-session move tends to continue or give back.

This strategy tests a **give-back** hypothesis with a focus on fading an
outsized bar only after a partial retracement confirms the reversion, but with
two key refinements over prior fade attempts:

1. The retracement is measured from the outsized bar's extreme, not its close.
2. The retracement must occur within a limited number of bars (default 3) after
   the outsized bar, preventing stale signals.

Additionally, we require that the outsized bar itself show signs of exhaustion:
for an up move, the close must be in the lower half of the bar's range; for a
down move, the close must be in the upper half. This filters for moves that
already began to give back within the outsized bar itself, which should increase
the probability of further reversion.

The mechanism is claimed to operate throughout the session: temporary imbalances
should revert regardless of hour, month, or weekday.

Key design choices:
- Rolling ATR (default 20 bars) adapts to current volatility.
- Outsized bar: range >= range_mult * ATR (default 2.0).
- Exhaustion filter: close in the lower 50% of the range for up moves, upper 50%
  for down moves.
- Retracement confirmation: for an up move, the current bar's low must fall below
  the outsized bar's high minus retrace_frac * outsized_range; for a down move,
  the current bar's high must rise above the outsized bar's low plus
  retrace_frac * outsized_range.
- Time limit: the retracement must occur within `max_wait` bars after the
  outsized bar, otherwise the signal expires.
- Entry is on the bar that confirms the retracement, in the opposite direction.
- Bracket exits use ATR-scaled offsets, with target larger than stop to allow
  the reversion to run.
"""

from __future__ import annotations


class Strategy:
    """Fade outsized moves after a retracement confirms reversion, with exhaustion filter."""

    def __init__(self, *, interval: str = "5m", atr_window: int = 20,
                 range_mult: float = 2.0, retrace_frac: float = 0.5,
                 max_wait: int = 3, exhaustion_frac: float = 0.5,
                 stop_atr: float = 1.0, target_atr: float = 2.0,
                 size: int = 1):
        self.interval = interval
        self.atr_window = atr_window
        self.range_mult = range_mult
        self.retrace_frac = retrace_frac
        self.max_wait = max_wait
        self.exhaustion_frac = exhaustion_frac
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.size = size

        # Per-session state.
        self._session = None
        self._trs: list[float] = []
        self._prev_close = None
        self._outsized_bar = None
        self._bars_since_outsized = 0

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
            self._bars_since_outsized = 0

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

        # Check for retracement if we have a pending outsized bar.
        if self._outsized_bar is not None:
            self._bars_since_outsized += 1

            # Expire the signal if we've waited too long.
            if self._bars_since_outsized > self.max_wait:
                self._outsized_bar = None
                self._bars_since_outsized = 0
            else:
                outsized_bar = self._outsized_bar
                direction = 1 if outsized_bar.close > outsized_bar.open else -1
                outsized_range = outsized_bar.high - outsized_bar.low

                if direction > 0:
                    # Outsized up move: look for a down retracement.
                    retrace_level = outsized_bar.high - self.retrace_frac * outsized_range
                    if bar.low <= retrace_level:
                        # Confirmed retracement: fade the up move with a short.
                        ctx.sell(
                            size=self.size,
                            stop_offset=self.stop_atr * atr,
                            target_offset=self.target_atr * atr,
                            tag="fade-retrace-short",
                        )
                        self._outsized_bar = None
                        self._bars_since_outsized = 0
                else:
                    # Outsized down move: look for an up retracement.
                    retrace_level = outsized_bar.low + self.retrace_frac * outsized_range
                    if bar.high >= retrace_level:
                        # Confirmed retracement: fade the down move with a long.
                        ctx.buy(
                            size=self.size,
                            stop_offset=self.stop_atr * atr,
                            target_offset=self.target_atr * atr,
                            tag="fade-retrace-long",
                        )
                        self._outsized_bar = None
                        self._bars_since_outsized = 0

        # Identify new outsized bars, applying the exhaustion filter.
        bar_range = bar.high - bar.low
        if bar_range >= self.range_mult * atr:
            # Exhaustion filter: for an up move, close in lower half; for down,
            # close in upper half.
            if bar.close > bar.open:
                # Up move: require close in lower exhaustion_frac of the range.
                close_position = (bar.close - bar.low) / bar_range
                if close_position <= self.exhaustion_frac:
                    self._outsized_bar = bar
                    self._bars_since_outsized = 0
            else:
                # Down move: require close in upper exhaustion_frac of the range.
                close_position = (bar.high - bar.close) / bar_range
                if close_position <= self.exhaustion_frac:
                    self._outsized_bar = bar
                    self._bars_since_outsized = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
