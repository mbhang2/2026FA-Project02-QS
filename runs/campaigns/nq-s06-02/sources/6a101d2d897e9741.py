"""Opening-range breakout with a stated hold/fade condition and a deadline.

The question: when price leaves the first 30 minutes of the regular-hours
session, does the move hold or does the range win? This strategy answers
"the move holds" for a stated, testable condition: the breakout bar must
close beyond the range, and the range must be wide enough to matter
relative to recent volatility.

Range: first 30 minutes of the session (six 5m bars), anchored to the
session open. Leaving: the bar's close is beyond the range high or low,
measured against the range width itself (not trailing volatility).
Direction: trade the breakout direction. Exit: a bracket with stop and
target, plus a hard deadline of 12 bars after entry, after which the
position is closed at market. This makes the answer readable in the
ordinary session: most sessions either produce no signal or a decided
exit within the deadline.

The strategy is deliberately simple, with no multi-stage confirmation.
It acts on every session once the range is complete, so the effect is
measured across the ordinary run of sessions rather than a handful.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", range_bars: int = 6,
                 size: int = 1, stop_mult: float = 0.5,
                 target_mult: float = 1.5, max_hold_bars: int = 12):
        self.interval = interval
        self.range_bars = range_bars
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.max_hold_bars = max_hold_bars

        # Per-session state, reset when the session changes.
        self._session = None
        self._session_bars = 0
        self._range_high = None
        self._range_low = None
        self._entry_session = None
        self._entry_bar = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Reset per-session state on a new session.
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_bars = 0
            self._range_high = None
            self._range_low = None

        self._session_bars += 1

        # Build the opening range from the first `range_bars` bars.
        if self._session_bars <= self.range_bars:
            bar = ctx.history(self.interval)[-1]
            if self._range_high is None or bar.high > self._range_high:
                self._range_high = bar.high
            if self._range_low is None or bar.low < self._range_low:
                self._range_low = bar.low
            return

        # We need a completed range.
        if self._range_high is None or self._range_low is None:
            return

        # Manage an open position: close it if the deadline has passed.
        if ctx.position:
            if self._entry_session != ctx.session:
                # Safety: should not happen, but if it does, close.
                ctx.close()
                return
            bars_held = self._session_bars - self._entry_bar
            if bars_held >= self.max_hold_bars:
                # Deadline reached. Cancel any working bracket orders and
                # flatten at market.
                for order in ctx.working_orders:
                    ctx.cancel(order.id)
                ctx.close()
            return

        # No position. Check for working orders (e.g., a bracket from a
        # previous entry that has not yet filled). If any, do nothing.
        if ctx.working_order_ids:
            return

        # Only act when actionable (not warmup).
        if not ctx.actionable:
            return

        # We need the bar that just closed.
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        last = history[-1]

        range_width = self._range_high - self._range_low
        if range_width <= 0:
            return

        # Signal: a close beyond the opening range.
        if last.close > self._range_high:
            # Breakout to the upside. Stop and target are distances from
            # the entry price, which will be the fill price.
            stop_offset = range_width * self.stop_mult
            target_offset = range_width * self.target_mult
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="breakout-long",
            )
            self._entry_session = ctx.session
            self._entry_bar = self._session_bars
        elif last.close < self._range_low:
            stop_offset = range_width * self.stop_mult
            target_offset = range_width * self.target_mult
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="breakout-short",
            )
            self._entry_session = ctx.session
            self._entry_bar = self._session_bars

    def on_fill(self, ctx) -> None:
        # Nothing to do on fill; the bracket and deadline handle exits.
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
