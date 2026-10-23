"""Proposal: #61 with entry reference changed from the first partial fill to
the engine's `ctx.average_entry_price`.

The highest-scoring prior strategy (#61, +58.7881) places its protective stop
and computes breakeven/trail levels from the price of the first entry fill.
When a market order fills in multiple partial fills (which is likely with
size=3 under the unfavorable fill model), later fills can be worse than the
first, so the first-fill price is not the correct average entry.  That makes
the initial stop/breakeven/trail levels slightly too far from the true average
for longs (and similarly for shorts), changing realized risk and reward.

This strategy keeps #61's exact entry signal, ATR calculation, trailing
cadence, and parameters.  The only change is that on every fill that
increases the position, `self.entry_price` is set from
`ctx.average_entry_price`, and the protective stop is placed/updated at
avg_entry +/- stop_mult * ATR.  The breakeven and chandelier trail then use
the same average entry.  This is the corrected reference point the engine
already provides, and it should slightly reduce adverse excursions on
multi-fill entries without changing trade frequency.
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
        self._last_position = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.slow)
        ctx.subscribe(self.fast)

    def _average_true_range(self, window) -> float | None:
        """Average true range over `window`.

        `window[0]` is used only as the previous close for `window[1]`, so the
        caller must pass one leading bar.
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

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        self._last_position = ctx.position

        slow_closed = self.slow in ctx.bars

        # Manage an open position on every bar close, fast or slow.
        if ctx.position:
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

        # Full pre-breakout prefix includes one leading close so every one of
        # the `lookback` prior bars contributes a true range.
        prefix = window[:-1]
        prior = prefix[-self.lookback:]
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

        recent = prior[-self.breakout_bars:]
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

            # The breakeven move waits for a slow-bar close.  The trail still
            # updates on every fast-bar close.
            if slow_closed and last >= entry_price + self.breakeven_atr_mult * atr:
                new_trigger = max(new_trigger, entry_price)
            if last >= entry_price + self.trail_atr_mult * atr:
                trail = last - self.trail_atr_distance_mult * atr
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
            if last <= entry_price - self.trail_atr_mult * atr:
                trail = last + self.trail_atr_distance_mult * atr
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
            return

        # Handle a partial exit: the engine may have reduced the position while
        # the protective stop is still working.  Resize the stop to the new
        # position so it never closes more than is open.
        if abs(fill.position) < abs(previous_position):
            existing = ctx.find_orders(tag="protective-stop")
            if existing and existing[0].size != abs(fill.position):
                ctx.modify(existing[0].id, size=abs(fill.position))
            return

        # Ignore fills that do not increase the position (already handled above).
        if abs(fill.position) <= abs(previous_position):
            return

        # Use the engine's average entry price rather than the first partial
        # fill's price.  This is the key correction over #61.
        avg = ctx.average_entry_price
        if not avg:
            avg = fill.price

        self.entry_price = float(avg)
        if self.entry_atr is None:
            self.entry_atr = self.pending_atr

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
                    trigger=self.entry_price - distance,
                    tif="GTC",
                    reduce_only=True,
                    tag="protective-stop",
                )
            elif fill.position < 0:
                ctx.buy(
                    size=abs(fill.position),
                    type="stop",
                    trigger=self.entry_price + distance,
                    tif="GTC",
                    reduce_only=True,
                    tag="protective-stop",
                )
        else:
            stop_order = existing[0]
            current = stop_order.stop_price
            new_trigger = (
                self.entry_price - distance
                if fill.position > 0
                else self.entry_price + distance
            )

            need_size = stop_order.size != abs(fill.position)
            need_trigger = (
                current is None
                or (fill.position > 0 and new_trigger > current)
                or (fill.position < 0 and new_trigger < current)
            )

            if need_trigger and need_size:
                ctx.modify(stop_order.id, trigger=new_trigger, size=abs(fill.position))
            elif need_trigger:
                ctx.modify(stop_order.id, trigger=new_trigger)
            elif need_size:
                ctx.modify(stop_order.id, size=abs(fill.position))

    def on_lifecycle(self, ctx) -> None:
        pass
