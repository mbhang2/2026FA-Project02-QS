"""Investigate whether an outsized move inside a session continues or gives back.

This strategy tests a continuation hypothesis with a delayed entry after a
consolidation bar, but with two important changes from #17 (the best so far):

1. The entry is a limit order at the consolidation bar's close, rather than a
   market order on the next bar. This reduces the cost of the unfavorable fill
   model, which requires a trade strictly through the limit. A limit order at
   the consolidation close is more likely to fill at a better price than a
   market order on the next bar.

2. The stop is placed beyond the consolidation bar's extreme, not based on the
   current bar's range. This gives the trade more room to breathe and avoids
   being stopped out by noise.

The trigger is a move from the session open that is large relative to the
session's range so far. After the trigger, we wait for a consolidation bar whose
range is at most a fraction of the trigger bar's range. We then place a limit
order at the consolidation bar's close in the direction of the move. The stop
is placed beyond the consolidation bar's extreme, and the target is a multiple
of the stop distance.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation after an outsized move followed by consolidation.

    An outsized move is one whose distance from the session open, relative to
    the session's range so far, exceeds a threshold. After such a move, we wait
    for a consolidation bar whose range is at most a fraction of the trigger
    bar's range. We then place a limit order at the consolidation bar's close
    in the direction of the move. The stop is placed beyond the consolidation
    bar's extreme, and the target is a multiple of the stop distance.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        threshold: float = 0.7,
        consolidation_fraction: float = 0.5,
        size: int = 1,
        stop_mult: float = 1.0,
        target_mult: float = 2.0,
        limit_offset: float = 0.0,
    ):
        self.interval = interval
        self.threshold = threshold
        self.consolidation_fraction = consolidation_fraction
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.limit_offset = limit_offset

        # Session-level state, kept across callbacks.
        self._session = None
        self._session_open: float | None = None
        self._session_high: float | None = None
        self._session_low: float | None = None

        # State for the trigger and consolidation.
        self._trigger_direction: int = 0  # 1 for long move, -1 for short move
        self._trigger_range: float | None = None  # range of the trigger bar
        self._consolidation_pending: bool = False  # True if we saw a consolidation bar
        self._consolidation_extreme: float | None = None  # extreme of consolidation bar

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        bar = history[-1]

        # Track session boundaries. Bars never span a session, so a change in
        # ctx.session means this is the first bar of a new session.
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_open = bar.open
            self._session_high = bar.high
            self._session_low = bar.low
            self._trigger_direction = 0
            self._trigger_range = None
            self._consolidation_pending = False
            self._consolidation_extreme = None
            return

        # These are not None after the first bar of a session.
        self._session_high = max(self._session_high, bar.high)
        self._session_low = min(self._session_low, bar.low)

        # If we have a pending consolidation from the previous bar, place a
        # limit order at the consolidation bar's close.
        if self._consolidation_pending:
            direction = self._trigger_direction
            consolidation_extreme = self._consolidation_extreme

            if consolidation_extreme is None:
                self._consolidation_pending = False
                return

            # Place a limit order at the consolidation bar's close, with a
            # small offset to improve fill probability.
            if direction > 0:
                limit_price = bar.close + self.limit_offset
                stop_price = consolidation_extreme - (bar.high - bar.low) * self.stop_mult
                target_price = limit_price + (limit_price - stop_price) * self.target_mult
                ctx.buy(
                    size=self.size,
                    type="limit",
                    limit=limit_price,
                    stop=stop_price,
                    target=target_price,
                    tag="continuation-consolidation-long",
                )
            else:
                limit_price = bar.close - self.limit_offset
                stop_price = consolidation_extreme + (bar.high - bar.low) * self.stop_mult
                target_price = limit_price - (stop_price - limit_price) * self.target_mult
                ctx.sell(
                    size=self.size,
                    type="limit",
                    limit=limit_price,
                    stop=stop_price,
                    target=target_price,
                    tag="continuation-consolidation-short",
                )

            # Clear the state.
            self._trigger_direction = 0
            self._trigger_range = None
            self._consolidation_pending = False
            self._consolidation_extreme = None
            return

        # If we have a pending trigger, check for a consolidation bar.
        if self._trigger_direction != 0 and self._trigger_range is not None:
            bar_range = bar.high - bar.low
            if bar_range <= 0:
                return

            # A consolidation bar has a range that is at most a fraction of the
            # trigger bar's range.
            if bar_range <= self._trigger_range * self.consolidation_fraction:
                self._consolidation_pending = True
                # Store the extreme of the consolidation bar for the stop.
                if self._trigger_direction > 0:
                    self._consolidation_extreme = bar.low
                else:
                    self._consolidation_extreme = bar.high
                return

            # If the bar continues in the move direction, update the trigger.
            if self._trigger_direction > 0 and bar.close > bar.open:
                self._trigger_range = bar_range
                return
            elif self._trigger_direction < 0 and bar.close < bar.open:
                self._trigger_range = bar_range
                return

            # If the bar reverses strongly, clear the trigger.
            self._trigger_direction = 0
            self._trigger_range = None
            return

        # No pending trigger: check for a new outsized move.
        session_range = self._session_high - self._session_low
        if session_range <= 0:
            return

        if self._session_open is None:
            return

        move = bar.close - self._session_open
        relative_move = abs(move) / session_range

        if relative_move < self.threshold:
            return

        # Store the trigger for the next bar.
        self._trigger_direction = 1 if move > 0 else -1
        self._trigger_range = bar.high - bar.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
