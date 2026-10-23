"""Proposal: #61 with a stricter rolling-extreme chandelier trail.

Keeps #61's 5m/1m breakout/trend entry, ATR-based protective stop, and
slow-close breakeven exactly.  The change is in the trailing logic: instead of
computing the chandelier stop from the current `ctx.last_price` at each 1m close,
the strategy now tracks the highest high / lowest low since entry and trails
from that rolling extreme.  The activation threshold and initial trail distance
remain the same (0.75 ATR / 0.50 ATR), so this should reduce give-back on
strong intra-interval moves without widening the stop the way the losing
late-runner widening did.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        slow: str = "5m",
        fast: str = "1m",
        lookback: int = 20,
        breakout_bars: int = 3,
        stop_mult: float = 0.90,
        breakeven_atr_mult: float = 0.30,
        trail_atr_mult: float = 0.75,
        trail_atr_distance_mult: float = 0.50,
        size: int = 3,
    ):
        self.slow = slow
        self.fast = fast
        self.lookback = lookback
        self.breakout_bars = breakout_bars
        self.stop_mult = stop_mult
        self.breakeven_atr_mult = breakeven_atr_mult
        self.trail_atr_mult = trail_atr_mult
        self.trail_atr_distance_mult = trail_atr_distance_mult
        self.size = size

        self.entry_price: float | None = None
        self.entry_atr: float | None = None
        self.pending_atr: float | None = None

        # Rolling extreme-price trail state, reset on each new entry.
        self.best_price: float | None = None
        self.worst_price: float | None = None

        self._last_position = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.slow)
        ctx.subscribe(self.fast)

    def _average_true_range(self, window) -> float | None:
        """Average true range over `window`.

        `window[0]` is used only as the previous close for `window[1]`.
        """
        trs = []
        for i in range(1, len(window)):
            bar = window[i]
            prev_close = window[i - 1].close
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
            trs.append(tr)
        if not trs:
            return None
        return sum(trs) / len(trs)

    def _update_extremes(self, ctx) -> None:
        """Track the best observed price for the open position."""
        if ctx.position > 0:
            best = self.best_price
            if best is None:
                best = ctx.last_price
            best = max(best, ctx.last_price)
            for bar in ctx.closed:
                best = max(best, bar.high)
            self.best_price = best
        elif ctx.position < 0:
            worst = self.worst_price
            if worst is None:
                worst = ctx.last_price
            worst = min(worst, ctx.last_price)
            for bar in ctx.closed:
                worst = min(worst, bar.low)
            self.worst_price = worst

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        self._last_position = ctx.position

        slow_closed = self.slow in ctx.bars

        # Manage an open position on every bar close, fast or slow.
        if ctx.position:
            self._update_extremes(ctx)
            self._manage_open_position(ctx, slow_closed=slow_closed)
            return

        if ctx.working_order_ids:
            return

        # New entries are only taken on the slow bar close.
        if not slow_closed:
            return

        history = ctx.history(self.slow)
        if len(history) < self.lookback + 2:
            return

        window = history.last(self.lookback + 2)
        if len(window) < self.lookback + 2:
            return

        last = window[-1]

        prefix = window[:-1]
        prior = prefix[-self.lookback :]
        if len(prior) < self.lookback:
            return

        trend = sum(b.close for b in prior) / self.lookback

        atr_value = self._average_true_range(prefix)
        if atr_value is None or atr_value <= 0:
            return
        atr = atr_value

        stop_distance = atr * self.stop_mult
        if stop_distance <= 0:
            return

        recent = prior[-self.breakout_bars :]
        recent_high = max(b.high for b in recent)
        recent_low = min(b.low for b in recent)

        last_delta = last.delta
        recent_delta = sum(b.delta for b in recent)

        if (
            last.close > last.open
            and last_delta > 0
            and recent_delta > 0
            and last.close > trend
            and last.close > recent_high
        ):
            self.pending_atr = atr
            ctx.buy(size=self.size, tag="trend-long")
        elif (
            last.close < last.open
            and last_delta < 0
            and recent_delta < 0
            and last.close < trend
            and last.close < recent_low
        ):
            self.pending_atr = atr
            ctx.sell(size=self.size, tag="trend-short")

    def _manage_open_position(self, ctx, *, slow_closed: bool) -> None:
        entry_price = self.entry_price
        entry_atr = self.entry_atr
        if entry_price is None or entry_atr is None:
            return

        atr = entry_atr
        last = ctx.last_price

        if ctx.position > 0:
            stops = ctx.find_orders(tag="protective-stop")
            if not stops:
                return
            stop_order = stops[0]
            current = stop_order.stop_price
            if current is None:
                return

            new_trigger = current

            if slow_closed and last >= entry_price + self.breakeven_atr_mult * atr:
                new_trigger = max(new_trigger, entry_price)

            highest = self.best_price if self.best_price is not None else last
            if highest >= entry_price + self.trail_atr_mult * atr:
                trail = highest - self.trail_atr_distance_mult * atr
                new_trigger = max(new_trigger, trail)

            if new_trigger > current:
                ctx.modify(stop_order.id, trigger=new_trigger)

        elif ctx.position < 0:
            stops = ctx.find_orders(tag="protective-stop")
            if not stops:
                return
            stop_order = stops[0]
            current = stop_order.stop_price
            if current is None:
                return

            new_trigger = current

            if slow_closed and last <= entry_price - self.breakeven_atr_mult * atr:
                new_trigger = min(new_trigger, entry_price)

            lowest = self.worst_price if self.worst_price is not None else last
            if lowest <= entry_price - self.trail_atr_mult * atr:
                trail = lowest + self.trail_atr_distance_mult * atr
                new_trigger = min(new_trigger, trail)

            if new_trigger < current:
                ctx.modify(stop_order.id, trigger=new_trigger)

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        previous_position = self._last_position
        self._last_position = fill.position

        if fill.position == 0:
            self.entry_price = None
            self.entry_atr = None
            self.pending_atr = None
            self.best_price = None
            self.worst_price = None
            return

        if abs(fill.position) <= abs(previous_position):
            return

        if self.entry_price is None:
            self.entry_price = fill.price
            self.entry_atr = self.pending_atr
            if fill.position > 0:
                self.best_price = fill.price
                self.worst_price = None
            elif fill.position < 0:
                self.worst_price = fill.price
                self.best_price = None

        if self.entry_atr is None:
            return

        distance = self.entry_atr * self.stop_mult
        if distance <= 0:
            return

        existing = ctx.find_orders(tag="protective-stop")
        if not existing:
            if fill.position > 0:
                ctx.sell(
                    size=abs(fill.position),
                    type="stop",
                    trigger=fill.price - distance,
                    tif="GTC",
                    reduce_only=True,
                    tag="protective-stop",
                )
            elif fill.position < 0:
                ctx.buy(
                    size=abs(fill.position),
                    type="stop",
                    trigger=fill.price + distance,
                    tif="GTC",
                    reduce_only=True,
                    tag="protective-stop",
                )
        else:
            stop_order = existing[0]
            if stop_order.size != abs(fill.position):
                ctx.modify(stop_order.id, size=abs(fill.position))

    def on_lifecycle(self, ctx) -> None:
        pass
