"""Investigate whether an outsized move inside a session continues or gives back.

This strategy tests a continuation hypothesis with a delayed entry after a
consolidation bar, building on the best prior result (#17, score +0.1321).

Key changes from #17:
1. The trigger is based on the move from the session's *median* price rather
   than the session open. The median is more robust to the opening auction and
   early session noise.
2. The consolidation bar must close in the direction of the move, not just
   have a small range. This filters out consolidation bars that are actually
   reversals.
3. The stop is placed beyond the consolidation bar's extreme, not based on the
   current bar's range. This gives the trade more room to breathe.
4. The target is a multiple of the stop distance, but capped at a maximum of
   4 times the stop distance to avoid unrealistic targets.

The entry is a market order on the bar after the consolidation bar, in the
direction of the move. The stop is placed beyond the consolidation bar's
extreme, and the target is a multiple of the stop distance.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation after an outsized move followed by consolidation.

    An outsized move is one whose distance from the session's median price,
    relative to the session's range so far, exceeds a threshold. After such a
    move, we wait for a consolidation bar whose range is at most a fraction of
    the trigger bar's range and whose close is in the direction of the move.
    We then enter on the next bar with a market order in the direction of the
    move. The stop is placed beyond the consolidation bar's extreme, and the
    target is a multiple of the stop distance.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        threshold: float = 0.6,
        consolidation_fraction: float = 0.4,
        size: int = 1,
        stop_mult: float = 1.0,
        target_mult: float = 2.0,
        max_target_mult: float = 4.0,
    ):
        self.interval = interval
        self.threshold = threshold
        self.consolidation_fraction = consolidation_fraction
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.max_target_mult = max_target_mult

        # Session-level state, kept across callbacks.
        self._session = None
        self._session_open: float | None = None
        self._session_high: float | None = None
        self._session_low: float | None = None
        self._session_prices: list[float] = []  # for median calculation

        # State for the trigger and consolidation.
        self._trigger_direction: int = 0  # 1 for long move, -1 for short move
        self._trigger_range: float | None = None  # range of the trigger bar
        self._consolidation_pending: bool = False  # True if we saw a consolidation bar
        self._consolidation_extreme: float | None = None  # extreme of consolidation bar

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _median(self, values: list[float]) -> float | None:
        if not values:
            return None
        sorted_values = sorted(values)
        n = len(sorted_values)
        if n % 2 == 1:
            return sorted_values[n // 2]
        return (sorted_values[n // 2 - 1] + sorted_values[n // 2]) / 2.0

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
            self._session_prices = [bar.close]
            self._trigger_direction = 0
            self._trigger_range = None
            self._consolidation_pending = False
            self._consolidation_extreme = None
            return

        # These are not None after the first bar of a session.
        self._session_high = max(self._session_high, bar.high)
        self._session_low = min(self._session_low, bar.low)
        self._session_prices.append(bar.close)

        # If we have a pending consolidation from the previous bar, enter now.
        if self._consolidation_pending:
            direction = self._trigger_direction
            consolidation_extreme = self._consolidation_extreme

            if consolidation_extreme is None:
                self._consolidation_pending = False
                return

            # Enter in the direction of the original move.
            if direction > 0:
                stop_distance = abs(bar.close - consolidation_extreme) * self.stop_mult
                target_distance = min(stop_distance * self.target_mult, stop_distance * self.max_target_mult)
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_distance,
                    target_offset=target_distance,
                    tag="continuation-consolidation-long",
                )
            else:
                stop_distance = abs(consolidation_extreme - bar.close) * self.stop_mult
                target_distance = min(stop_distance * self.target_mult, stop_distance * self.max_target_mult)
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_distance,
                    target_offset=target_distance,
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
            # trigger bar's range, and closes in the direction of the move.
            if bar_range <= self._trigger_range * self.consolidation_fraction:
                if self._trigger_direction > 0 and bar.close > bar.open:
                    self._consolidation_pending = True
                    self._consolidation_extreme = bar.low
                    return
                elif self._trigger_direction < 0 and bar.close < bar.open:
                    self._consolidation_pending = True
                    self._consolidation_extreme = bar.high
                    return

            # If the bar continues strongly in the move direction, update the
            # trigger range.
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

        median_price = self._median(self._session_prices)
        if median_price is None:
            return

        move = bar.close - median_price
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
