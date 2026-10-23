"""Strategy #27: #18 plus 1m management and high-water-mark chandelier trail.

#18 is this campaign's best so far (+42.9724).  It enters delta-confirmed
three-bar breakouts, size=3, with a protective stop placed on fill and a
chandelier trail.  Its ATR is computed from the pre-breakout lookback window.

The one weakness left in its exit management is that the trail is evaluated
only when a 5m bar closes, and it is computed from `ctx.last_price` -- the
last trade of that 5m bar -- not from the highest high / lowest low reached
since entry.  In a volatile up bar that closes near its low, a long position's
trail does not advance to the high, giving back the best prices the market
printed.

This version keeps #18's entry, ATR construction, protective-stop sizing and
trailing levels exactly, but adds a 1m subscription used only for management:
the high-water / low-water mark is updated from every closed bar and the
chandelier trail is advanced from that mark rather than from the last price.
Entries still occur only on 5m closes.
"""

from __future__ import annotations


class Strategy:
    """Delta-confirmed three-bar breakout, 1m-managed high-water chandelier trail."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        manage_interval: str = "1m",
        lookback: int = 20,
        breakout_bars: int = 3,
        stop_mult: float = 0.90,
        breakeven_atr_mult: float = 0.35,
        trail_atr_mult: float = 0.75,
        trail_atr_distance_mult: float = 0.50,
        size: int = 3,
    ):
        self.interval = interval
        self.manage_interval = manage_interval
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

        # The favorable extreme since entry: highest high for longs, lowest low
        # for shorts.  Stored on self because ctx is only valid in a callback.
        self._best_price: float | None = None

        self._last_position = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        # Used only to update the trail at a finer resolution.  Bar intervals
        # are cheap, and this keeps the original 5m entry signal unchanged.
        ctx.subscribe(self.manage_interval)

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

        # Entries still happen only on 5m closes.  A 1m callback while flat
        # is not a decision point for this strategy.
        if self.interval not in ctx.bars:
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

        # ATR for risk and trailing is computed from the pre-breakout window,
        # excluding the breakout bar itself, exactly as in #18.
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
        # Update the favorable extreme from every bar closed in this callback.
        # This includes the 1m bar and, when they coincide, the 5m bar.
        for bar in ctx.closed:
            if ctx.position > 0:
                if self._best_price is None or bar.high > self._best_price:
                    self._best_price = bar.high
            elif ctx.position < 0:
                if self._best_price is None or bar.low < self._best_price:
                    self._best_price = bar.low

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

            best = self._best_price if self._best_price is not None else last
            new_trigger = current

            if best >= entry_price + self.breakeven_atr_mult * atr:
                new_trigger = max(new_trigger, entry_price)
            if best >= entry_price + self.trail_atr_mult * atr:
                trail = best - self.trail_atr_distance_mult * atr
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

            worst = self._best_price if self._best_price is not None else last
            new_trigger = current

            if worst <= entry_price - self.breakeven_atr_mult * atr:
                new_trigger = min(new_trigger, entry_price)
            if worst <= entry_price - self.trail_atr_mult * atr:
                trail = worst + self.trail_atr_distance_mult * atr
                new_trigger = min(new_trigger, trail)

            if new_trigger < current:
                ctx.modify(stop_order.id, trigger=new_trigger)

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        previous_position = self._last_position
        self._last_position = fill.position

        if fill.position == 0:
            # If a flat close or a manual flatten left a protective stop
            # working, remove it so it cannot block the next entry.
            for order in ctx.find_orders(tag="protective-stop"):
                ctx.cancel(order.id)

            self.entry_price = None
            self.entry_atr = None
            self.pending_atr = None
            self._best_price = None
            return

        # A partial or reducing fill does not start a new entry and does not
        # change the original entry's risk parameters.
        if abs(fill.position) <= abs(previous_position):
            return

        if self.entry_price is None:
            self.entry_price = fill.price
            self.entry_atr = self.pending_atr
            self._best_price = fill.price

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
