"""Next strategy: keep #18's delta-confirmed three-bar breakout, pre-breakout
ATR, chandelier trailing, and partial-fill protective-stop handling, but make
the trailing side volatility-responsive.

#18 anchored the chandelier trail entirely on the entry ATR. If volatility
expands after entry, the fixed entry ATR can trigger the trailing stop too
early, leaving a winning trend before it has room to develop. #19 tried to make
the trail more responsive with bar extremes and got worse. That version
effectively tightened the trail; this one tightens nothing, and only *widens*
the trail when current volatility is higher than entry volatility.

Specifically:

- initial protective-stop distance and the breakeven threshold still use the
  pre-breakout entry ATR, so downside and scratch logic is unchanged from #18,
- the chandelier activation and trail distance use max(entry ATR, current ATR)
  from the latest lookback window.

Everything else is identical to #18.
"""

from __future__ import annotations


class Strategy:
    """Delta-confirmed three-bar breakout with volatility-responsive chandelier."""

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
        size: int = 3,
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

        # Pre-breakout ATR only, as in #18.
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
        entry_price = self.entry_price
        entry_atr = self.entry_atr
        if entry_price is None or entry_atr is None:
            return

        # Current ATR, used only to *widen* the trailing geometry when
        # volatility has expanded since entry. It never tightens it.
        current_window = ctx.history(self.interval).last(self.lookback + 1)
        current_atr = self._average_true_range(current_window)
        trail_atr = entry_atr
        if current_atr is not None and current_atr > trail_atr:
            trail_atr = current_atr

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

            # Breakeven still uses entry ATR: same downside protection as #18.
            if last >= entry_price + self.breakeven_atr_mult * entry_atr:
                new_trigger = max(new_trigger, entry_price)

            # Chandelier uses max(entry ATR, current ATR), so an expanding
            # trend gets a little more room.
            if last >= entry_price + self.trail_atr_mult * trail_atr:
                trail = last - self.trail_atr_distance_mult * trail_atr
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

            if last <= entry_price - self.breakeven_atr_mult * entry_atr:
                new_trigger = min(new_trigger, entry_price)

            if last <= entry_price - self.trail_atr_mult * trail_atr:
                trail = last + self.trail_atr_distance_mult * trail_atr
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

        if abs(fill.position) <= abs(previous_position):
            return

        if self.entry_price is None:
            self.entry_price = fill.price
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
