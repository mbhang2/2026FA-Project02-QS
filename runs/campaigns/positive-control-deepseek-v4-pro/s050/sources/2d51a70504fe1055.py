from __future__ import annotations


class Strategy:
    """Proposal #32: #31 with true chandelier high/low trailing.

    The best-known strategy currently trails its protective stop from the
    *last trade price* observed on each bar close.  For a long position that
    means the trail is updated from the bar's close, not from the highest
    price actually traded during the rally.  If a bar sends price higher and
    then closes back down, the close-based trail fails to raise the stop and
    gives back more of the move than necessary.

    This version keeps the same corrected pre-breakout ATR, the same
    delta-confirmed three-bar breakout entry, and the same stop/trail
    multipliers, but maintains a true chandelier:
      - longs trail from `highest_high - trail_atr_distance_mult * ATR`
      - shorts trail from `lowest_low + trail_atr_distance_mult * ATR`

    Highest-high/lowest-low are updated from the current closed bar's actual
    high/low, which is available in `ctx.bars` at the bar close without
    lookahead.  The breakeven trigger is likewise referenced to the high/low
    rather than to the last trade price.
    """

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
        self.highest_high: float | None = None
        self.lowest_low: float | None = None
        self._last_position = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _average_true_range(self, window) -> float | None:
        """Average true range over `window`.

        `window[0]` is used only as the previous close for `window[1]`, so
        the caller must pass one leading bar.
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

        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        if ctx.position:
            self._manage_open_position(ctx, bar)
            return

        if ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
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

        stop = atr * self.stop_mult
        if stop <= 0:
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

    def _manage_open_position(self, ctx, bar) -> None:
        entry_price = self.entry_price
        entry_atr = self.entry_atr
        if entry_price is None or entry_atr is None:
            return

        atr = entry_atr

        if ctx.position > 0:
            if self.highest_high is None:
                self.highest_high = entry_price
            self.highest_high = max(self.highest_high, bar.high)
            reference = self.highest_high

            stops = ctx.find_orders(tag="protective-stop")
            if not stops:
                return
            stop_order = stops[0]
            current = stop_order.stop_price
            if current is None:
                return

            new_trigger = current
            if reference >= entry_price + self.breakeven_atr_mult * atr:
                new_trigger = max(new_trigger, entry_price)
            if reference >= entry_price + self.trail_atr_mult * atr:
                trail = reference - self.trail_atr_distance_mult * atr
                new_trigger = max(new_trigger, trail)

            if new_trigger > current:
                ctx.modify(stop_order.id, trigger=new_trigger)

        elif ctx.position < 0:
            if self.lowest_low is None:
                self.lowest_low = entry_price
            self.lowest_low = min(self.lowest_low, bar.low)
            reference = self.lowest_low

            stops = ctx.find_orders(tag="protective-stop")
            if not stops:
                return
            stop_order = stops[0]
            current = stop_order.stop_price
            if current is None:
                return

            new_trigger = current
            if reference <= entry_price - self.breakeven_atr_mult * atr:
                new_trigger = min(new_trigger, entry_price)
            if reference <= entry_price - self.trail_atr_mult * atr:
                trail = reference + self.trail_atr_distance_mult * atr
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
            self.highest_high = None
            self.lowest_low = None
            return

        if self.entry_price is None:
            self.entry_price = fill.price
            self.entry_atr = self.pending_atr

        if self.entry_atr is None:
            return

        distance = self.entry_atr * self.stop_mult
        if distance <= 0:
            return

        target_size = abs(fill.position)

        existing = ctx.find_orders(tag="protective-stop")
        if not existing:
            if fill.position > 0:
                self.highest_high = fill.price
                ctx.sell(
                    size=target_size,
                    type="stop",
                    trigger=fill.price - distance,
                    tif="GTC",
                    reduce_only=True,
                    tag="protective-stop",
                )
            elif fill.position < 0:
                self.lowest_low = fill.price
                ctx.buy(
                    size=target_size,
                    type="stop",
                    trigger=fill.price + distance,
                    tif="GTC",
                    reduce_only=True,
                    tag="protective-stop",
                )
        else:
            stop_order = existing[0]
            if stop_order.size != target_size:
                ctx.modify(stop_order.id, size=target_size)

    def on_lifecycle(self, ctx) -> None:
        pass
