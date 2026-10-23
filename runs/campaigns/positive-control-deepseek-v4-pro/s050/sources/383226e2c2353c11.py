"""Next strategy: keep the #11 entry engine and chandelier trail exactly,
scale to two contracts, and tighten the trail slightly to lock more of the
winner.

Prior trials:
#0 simple close-to-close momentum: 1,241 trades, +3.3002
#1 ATR-scaled + short trend filter: 895 trades, +4.0299
#2 adding 20-bar range + volume filter: 527 trades, +3.0401
#3 one-bar breakout: 846 trades, +4.6289
#4 two-bar breakout: 830 trades, +4.8108
#5 three-bar breakout: 813 trades, +4.9760
#6 five-bar breakout: 770 trades, +4.7048
#7 three-bar breakout + cumulative breakout delta: 741 trades, +5.0112
#8 clearance buffer filtered nothing (identical trades to #7)
#9 over-filtered and was not scored: exposed in only 14 of 105 sessions
#10 keep #7 entry + breakeven/trailing stop management: 743 trades, +8.5310
#11 keep #7/#10 entry, no profit target, chandelier trailing: 234 trades, +21.7727

#11 already proved the entry and the no-target chandelier exit.  The next
increment is therefore not another filter -- filters have already shown they can
shrink session exposure -- but sizing.  The same positive-expectancy trades are
taken with two contracts, and the trail is tightened modestly:
breakeven after +0.35 ATR (was +0.50), trailing after +0.75 ATR (was +1.25),
trail distance 0.50 ATR (was 0.75), initial stop 0.90 ATR (was 1.00).
This keeps the entry engine identical, so session exposure should not drop.
"""

from __future__ import annotations


class Strategy:
    """Delta-confirmed three-bar breakout, chandelier trailing, two contracts."""

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
        size: int = 2,
    ):
        self.interval = interval
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

    def _manage_open_position(self, ctx) -> None:
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
            if last >= entry_price + self.breakeven_atr_mult * atr:
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

        distance = self.entry_atr * self.stop_mult

        if ctx.find_orders(tag="protective-stop"):
            return

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
