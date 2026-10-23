"""Proposal #46: #38 with a true high-water-mark chandelier trail.

The best prior strategy in this campaign, #38, runs ATR-based chandelier
trailing and breakeven logic on 1-minute bar closes. Its trailing formula is
based on `ctx.last_price`, so a bar whose high reaches the trailing threshold
but closes back below it never tightens the stop. The favorable excursion is
lost.

This proposal keeps #38's exact 5-minute entry logic, ATR calculation, initial
protective-stop placement, breakeven move, 1-minute management frequency and
size. It adds one refinement: track the highest high / lowest low since entry,
and use those watermarks for the chandelier trail. The watermark trail is only
applied when the resulting trigger remains on the correct side of the market
(below `last_price` for longs, above it for shorts), so it cannot turn into an
immediate market stop. The original `last_price`-based trail is kept intact as
the base case; the watermark can only tighten the stop further.
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
        breakeven_atr_mult: float = 0.35,
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

        self.high_since_entry: float | None = None
        self.low_since_entry: float | None = None

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

    def _update_watermarks(self, ctx) -> None:
        """Track the most favorable price seen since entry."""
        if not ctx.closed:
            return

        if ctx.position > 0:
            high = max(b.high for b in ctx.closed)
            if self.high_since_entry is None:
                self.high_since_entry = high
            elif high > self.high_since_entry:
                self.high_since_entry = high
            if ctx.last_price > self.high_since_entry:
                self.high_since_entry = ctx.last_price

        elif ctx.position < 0:
            low = min(b.low for b in ctx.closed)
            if self.low_since_entry is None:
                self.low_since_entry = low
            elif low < self.low_since_entry:
                self.low_since_entry = low
            if ctx.last_price < self.low_since_entry:
                self.low_since_entry = ctx.last_price

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        self._last_position = ctx.position

        # Manage an open position on every bar close, fast or slow.
        if ctx.position:
            self._update_watermarks(ctx)
            self._manage_open_position(ctx)
            return

        if ctx.working_order_ids:
            return

        # New entries are only taken on the slow bar close.
        if self.slow not in ctx.bars:
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
        prefix = window[:-1]              # length lookback + 1
        prior = prefix[-self.lookback:]    # the bars just before the breakout
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
                self._place_protective_stop(ctx, entry_price, atr)
                return
            stop_order = stops[0]
            current = stop_order.stop_price
            if current is None:
                return

            new_trigger = current

            # Original confirmed-close breakeven and last-price trail (kept
            # exactly from the best prior strategy).
            if last >= entry_price + self.breakeven_atr_mult * atr:
                new_trigger = max(new_trigger, entry_price)
            if last >= entry_price + self.trail_atr_mult * atr:
                trail = last - self.trail_atr_distance_mult * atr
                new_trigger = max(new_trigger, trail)

            # High-water chandelier refinement. It may tighten the stop only
            # when doing so still leaves the sell-stop below the current price.
            high = self.high_since_entry
            if high is not None and high >= entry_price + self.trail_atr_mult * atr:
                hw_trail = high - self.trail_atr_distance_mult * atr
                if hw_trail < last:
                    new_trigger = max(new_trigger, hw_trail)

            if new_trigger > current:
                ctx.modify(stop_order.id, trigger=new_trigger)

        elif ctx.position < 0:
            stops = ctx.find_orders(tag="protective-stop")
            if not stops:
                self._place_protective_stop(ctx, entry_price, atr)
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

            low = self.low_since_entry
            if low is not None and low <= entry_price - self.trail_atr_mult * atr:
                hw_trail = low + self.trail_atr_distance_mult * atr
                if hw_trail > last:
                    new_trigger = min(new_trigger, hw_trail)

            if new_trigger < current:
                ctx.modify(stop_order.id, trigger=new_trigger)

    def _place_protective_stop(self, ctx, entry_price: float, atr: float) -> None:
        """Arm a missing protective stop for an open position."""
        distance = atr * self.stop_mult
        if distance <= 0 or not ctx.position:
            return
        if ctx.position > 0:
            ctx.sell(
                size=abs(ctx.position),
                type="stop",
                trigger=entry_price - distance,
                tif="GTC",
                reduce_only=True,
                tag="protective-stop",
            )
        elif ctx.position < 0:
            ctx.buy(
                size=abs(ctx.position),
                type="stop",
                trigger=entry_price + distance,
                tif="GTC",
                reduce_only=True,
                tag="protective-stop",
            )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        previous_position = self._last_position
        self._last_position = fill.position

        if fill.position == 0:
            self.entry_price = None
            self.entry_atr = None
            self.pending_atr = None
            self.high_since_entry = None
            self.low_since_entry = None
            return

        if abs(fill.position) <= abs(previous_position):
            return

        if self.entry_price is None:
            self.entry_price = fill.price
            self.entry_atr = self.pending_atr
            if fill.position > 0:
                self.high_since_entry = fill.price
                self.low_since_entry = None
            elif fill.position < 0:
                self.low_since_entry = fill.price
                self.high_since_entry = None

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
