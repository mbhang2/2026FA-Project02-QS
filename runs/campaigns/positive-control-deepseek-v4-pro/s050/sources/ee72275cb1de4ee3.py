"""Next strategy: keep the #13 three-bar delta breakout entry, add a faster
1-minute management cadence so the chandelier trail is updated more promptly,
and keep all protective-stop parameters from the best prior trial.

Prior trial progression (training split):
#0 close-to-close momentum: 1,241 trades, +3.3002
#1 ATR-scaled + trend filter: 895 trades, +4.0299
#2 range + volume filter: 527 trades, +3.0401
#3 one-bar breakout: 846 trades, +4.6289
#4 two-bar breakout: 830 trades, +4.8108
#5 three-bar breakout: 813 trades, +4.9760
#6 five-bar breakout: 770 trades, +4.7048
#7 three-bar breakout + cumulative delta: 741 trades, +5.0112
#8 clearance buffer filtered nothing (identical to #7)
#9 over-filtered and not scored
#10 #7 entry + breakeven/trailing stop management: 743 trades, +8.5310
#11 #7/#10 entry, no profit target, chandelier trailing: 234 trades, +21.7727
#12 #11 entry and chandelier, size=2, tighter trail: 344 trades, +28.8905
#13 #11 entry and chandelier, size=3, tighter trail: 427 trades, +31.2924

#13 leaves the trailing stop to be updated only when a 5m bar closes.  If the
1m bar that closes inside that 5m period makes a new high and then pulls back
before the 5m close, the trail does not see the high at all.  Subscribing to 1m
bars purely for management lets the same chandelier schedule act on the finer
close grid, which should lock in more of a winner without changing the entry
engine or reducing session exposure.
"""

from __future__ import annotations


class Strategy:
    """Delta-confirmed three-bar breakout, chandelier trailing managed on 1m."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        fast_interval: str = "1m",
        lookback: int = 20,
        breakout_bars: int = 3,
        stop_mult: float = 0.90,
        breakeven_atr_mult: float = 0.35,
        trail_atr_mult: float = 0.75,
        trail_atr_distance_mult: float = 0.50,
        size: int = 3,
    ):
        self.interval = interval
        self.fast_interval = fast_interval
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

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        # Faster management cadence.  We never enter on this subscription,
        # but it lets the trailing stop respond to a new high/low before the
        # next 5m bar close.
        ctx.subscribe(self.fast_interval)

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

        # Management can run on every 1m/5m close while a position is open.
        if ctx.position:
            self._manage_open_position(ctx)
            return

        if ctx.working_order_ids:
            return

        # Entries are taken only when the slower interval closes.
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
        atr_value = self._average_true_range(window)
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

    def _place_protective_stop(self, ctx) -> None:
        """Arm protection if it is missing while a position is open."""
        entry_price = self.entry_price
        entry_atr = self.entry_atr
        if entry_price is None or entry_atr is None:
            return

        distance = entry_atr * self.stop_mult
        position = ctx.position

        if position > 0:
            ctx.sell(
                size=abs(position),
                type="stop",
                trigger=entry_price - distance,
                tif="GTC",
                reduce_only=True,
                tag="protective-stop",
            )
        elif position < 0:
            ctx.buy(
                size=abs(position),
                type="stop",
                trigger=entry_price + distance,
                tif="GTC",
                reduce_only=True,
                tag="protective-stop",
            )

    def _manage_open_position(self, ctx) -> None:
        entry_price = self.entry_price
        entry_atr = self.entry_atr
        if entry_price is None or entry_atr is None:
            return

        stops = ctx.find_orders(tag="protective-stop")
        if not stops:
            self._place_protective_stop(ctx)
            return

        stop_order = stops[0]
        current = stop_order.stop_price
        if current is None:
            return

        atr = entry_atr
        last = ctx.last_price

        if ctx.position > 0:
            new_trigger = current
            if last >= entry_price + self.breakeven_atr_mult * atr:
                new_trigger = max(new_trigger, entry_price)
            if last >= entry_price + self.trail_atr_mult * atr:
                trail = last - self.trail_atr_distance_mult * atr
                new_trigger = max(new_trigger, trail)

            if new_trigger > current:
                ctx.modify(stop_order.id, trigger=new_trigger)

        elif ctx.position < 0:
            new_trigger = current
            if last <= entry_price - self.breakeven_atr_mult * atr:
                new_trigger = min(new_trigger, entry_price)
            if last <= entry_price - self.trail_atr_mult * atr:
                trail = last + self.trail_atr_distance_mult * atr
                new_trigger = min(new_trigger, trail)

            if new_trigger < current:
                ctx.modify(stop_order.id, trigger=new_trigger)

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position == 0:
            self.entry_price = None
            self.entry_atr = None
            self.pending_atr = None
            return

        if self.entry_price is None:
            self.entry_price = fill.price
            self.entry_atr = self.pending_atr

        if self.entry_atr is None:
            return

        if ctx.find_orders(tag="protective-stop"):
            return

        distance = self.entry_atr * self.stop_mult

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

    def on_lifecycle(self, ctx) -> None:
        pass
