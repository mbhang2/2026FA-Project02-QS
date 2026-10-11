"""Probe continuation vs give-back with a time-symmetric, session-consistent design.

The previous best (#16) tested continuation with persistence and a pullback
entry, and scored +0.0603. The prior give-back attempt (#15) scored -0.1769.
This strategy sharpens the continuation idea by requiring the outsized move to
occur **early in the session**, where information-driven continuation should be
strongest, and by using a cleaner confirmation: the outsized bar must be
followed by a bar that closes beyond the outsized bar's extreme, then we enter
on a pullback that does not retrace the whole move.

Key changes from #16:
- Add a session-time filter: only consider outsized bars before a fraction of
  the session has elapsed (default 0.6). This avoids late-session noise and
  respects the question's demand for a mechanism that operates predictably.
- Confirmation is now a **breakout** bar (close beyond the outsized bar's
  high/low) rather than a count of continuation bars. This is a stronger,
  simpler persistence signal.
- Pullback entry requires the pullback bar to stay within the outsized bar's
  range, as before, but now uses a tighter fraction (0.35) to ensure we are
  entering near the breakout level.
- Use ATR-scaled brackets with a target that is 2.5x the stop, giving the
  continuation room to run while keeping risk controlled.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of early-session outsized moves with breakout confirmation."""

    def __init__(self, *, interval: str = "5m", atr_window: int = 20,
                 range_mult: float = 2.0, session_frac: float = 0.6,
                 pullback_frac: float = 0.35, stop_atr: float = 1.0,
                 target_atr: float = 2.5, size: int = 1):
        self.interval = interval
        self.atr_window = atr_window
        self.range_mult = range_mult
        self.session_frac = session_frac
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
            if self._direction > 0 and bar.close < bar.open:
                # Down pullback in an up move.
                if bar.low >= self._outsized_bar.low + self.pullback_frac * (self._outsized_bar.high - self._outsized_bar.low):
                    ctx.buy(
                        size=self.size,
                        stop_offset=self.stop_atr * atr,
                        target_offset=self.target_atr * atr,
                        tag="continuation-breakout-pullback-long",
                    )
                    self._reset_signal()
            elif self._direction < 0 and bar.close > bar.open:
                # Up pullback in a down move.
                if bar.high <= self._outsized_bar.high - self.pullback_frac * (self._outsized_bar.high - self._outsized_bar.low):
                    ctx.sell(
                        size=self.size,
                        stop_offset=self.stop_atr * atr,
                        target_offset=self.target_atr * atr,
                        tag="continuation-breakout-pullback-short",
                    )
                    self._reset_signal()
            return

        # Track breakout confirmation after an outsized bar.
        if self._outsized_bar is not None:
            if self._direction > 0 and bar.close > self._outsized_bar.high:
                self._pending_pullback = True
            elif self._direction < 0 and bar.close < self._outsized_bar.low:
                self._pending_pullback = True
            else:
                # If the bar closes back inside the outsized bar, the breakout
                # failed; reset the signal.
                self._reset_signal()

        # Identify new outsized bars, but only early in the session.
        session_elapsed = (ctx.ts - ctx.session_start) / (ctx.session_end - ctx.session_start)
        if session_elapsed < self.session_frac:
            bar_range = bar.high - bar.low
            if bar_range >= self.range_mult * atr:
                self._outsized_bar = bar
                self._direction = 1 if bar.close > bar.open else -1
                self._pending_pullback = False

    def _reset_signal(self) -> None:
        self._outsized_bar = None
        self._direction = 0
        self._pending_pullback = False

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
