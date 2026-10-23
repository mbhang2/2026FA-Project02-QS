"""#28: #18 plus a true 5m high-water chandelier exit, with a clamp.

#18 is this campaign's best so far (+42.9724).  It enters delta-confirmed
three-bar breakouts, size=3, places a protective stop on fill, and trails that
stop from `ctx.last_price` at each 5m close.  That means a long position does
not advance its stop for the high of a bar that closes off its high, giving
back the best prices printed during the bar.

#27 tried to fix this with a 1m high-water mark and was worse.  The likely
reason is that 1m extremes are noisy and, critically, the unclamped trail could
put a protective stop above/below the market, making it immediately marketable
and causing churn.

This version keeps #18's entry, ATR construction, protective-stop sizing,
breakeven and trailing levels exactly, but updates the chandelier extreme from
the closed **5m** bar high/low rather than from `ctx.last_price`, and clamps the
new stop so it is always at least `min_buffer_atr_mult` ATRs above/below the
most recent close.  That captures favourable 5m extremes without introducing a
1m subscription or an immediately marketable trigger.
"""

from __future__ import annotations


class Strategy:
    """Delta-confirmed three-bar breakout, 5m high-water chandelier trail."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 20,
        breakout_bars: int = 3,
        stop_mult: float = 0.90,
        breakeven_atr_mult: float = 0.35,
        trail_atr_mult: float = 0.75,
        trail_atr_distance_mult: float = 0.50,
        min_buffer_atr_mult: float = 0.25,
        size: int = 3,
    ):
        self.interval = interval
        self.lookback = lookback
        self.breakout_bars = breakout_bars
        self.stop_mult = stop_mult
        self.breakeven_atr_mult = breakeven_atr_mult
        self.trail_atr_mult = trail_atr_mult
        self.trail_atr_distance_mult = trail_atr_distance_mult
        self.min_buffer_atr_mult = min_buffer_atr_mult
        self.size = size

        self.entry_price: float | None = None
        self.entry_atr: float | None = None
        self.pending_atr: float | None = None
        self._best_price: float | None = None
        self._worst_price: float | None = None
        self._last_position = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _average_true_range(self, window) -> float | None:
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

        if ctx.position:
            self._manage_open_position(ctx)
            return

        if ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        window = history.last(self.lookback + 1)
        if len(window) < self.lookback + 1:
            return
        if len(window) < self.breakout_bars + 1:
            return

        last = window[-1]
        prior = window[:-1]

        trend = sum(b.close for b in prior) / self.lookback

        # Exactly as in #18: ATR for risk and trailing is computed from the
        # pre-breakout window, excluding the breakout bar itself.
        atr_value = self._average_true_range(prior)
        if atr_value is None or atr_value <= 0:
            return
        atr = atr_value

        stop = atr * self.stop_mult
        if stop <= 0:
            return

        recent = window[-self.breakout_bars - 1:-1]
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

    def _manage_open_position(self, ctx) -> None:
        bar = ctx.bars.get(self.interval)
        if bar is None:
            hist = ctx.history(self.interval)
            if len(hist) < 1:
                return
            bar = hist[-1]

        # Update the favourable extreme from this closed 5m bar.  Fills happen
        # before the bar callback, so the entry bar's extreme is included.
        if ctx.position > 0:
            if self._best_price is None:
                self._best_price = bar.high
            else:
                self._best_price = max(self._best_price, bar.high)
        elif ctx.position < 0:
            if self._worst_price is None:
                self._worst_price = bar.low
            else:
                self._worst_price = min(self._worst_price, bar.low)

        entry_price = self.entry_price
        entry_atr = self.entry_atr
        if entry_price is None or entry_atr is None:
            return

        atr = entry_atr
        last = bar.close

        if ctx.position > 0:
            stops = ctx.find_orders(tag="protective-stop")
            if not stops:
                return
            stop_order = stops[0]
            current = stop_order.stop_price
            if current is None:
                return

            best = self._best_price if self._best_price is not None else last
            new_trigger = current

            if best >= entry_price + self.breakeven_atr_mult * atr:
                new_trigger = max(new_trigger, entry_price)
            if best >= entry_price + self.trail_atr_mult * atr:
                trail = best - self.trail_atr_distance_mult * atr
                # Never make the stop immediately marketable.
                cap = last - self.min_buffer_atr_mult * atr
                trail = min(trail, cap)
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

            worst = self._worst_price if self._worst_price is not None else last
            new_trigger = current

            if worst <= entry_price - self.breakeven_atr_mult * atr:
                new_trigger = min(new_trigger, entry_price)
            if worst <= entry_price - self.trail_atr_mult * atr:
                trail = worst + self.trail_atr_distance_mult * atr
                # Never make the stop immediately marketable.
                cap = last + self.min_buffer_atr_mult * atr
                trail = max(trail, cap)
                new_trigger = min(new_trigger, trail)

            if new_trigger < current:
                ctx.modify(stop_order.id, trigger=new_trigger)

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        previous_position = self._last_position
        self._last_position = fill.position

        if fill.position == 0:
            # If a protective stop is somehow still working after flattening,
            # remove it so it cannot block the next entry.
            for order in ctx.find_orders(tag="protective-stop"):
                ctx.cancel(order.id)

            self.entry_price = None
            self.entry_atr = None
            self.pending_atr = None
            self._best_price = None
            self._worst_price = None
            return

        # A partial or reducing fill does not start a new entry and does not
        # change the original entry's risk parameters.
        if abs(fill.position) <= abs(previous_position):
            return

        if self.entry_price is None:
            self.entry_price = fill.price
            self.entry_atr = self.pending_atr
            if fill.position > 0:
                self._best_price = fill.price
                self._worst_price = None
            elif fill.position < 0:
                self._worst_price = fill.price
                self._best_price = None

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
            if stop_order.size < abs(fill.position):
                ctx.modify(stop_order.id, size=abs(fill.position))

    def on_lifecycle(self, ctx) -> None:
        pass
