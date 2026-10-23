"""Proposal: #38 with a partial profit-taking limit.

The best previous strategy (#38) enters on a 5-minute ATR/delta breakout and
manages a single protective stop with breakeven and chandelier trailing on
1-minute closes. It rides the full position until the trailing stop exits.

This keeps every entry filter, ATR calculation, trailing stop, and the 1-minute
management exactly as #38. The change is to layer a small resting take-profit
limit after entry: one of the three contracts is offered at a fixed multiple of
entry ATR. When it fills, the protective stop is resized down to the remaining
position, and the other two contracts continue with the existing trail.

The effect is to bank some profit on the fastest part of a favorable move while
still leaving the core position exposed to a larger trend. Position size, entry
filters and trailing parameters are otherwise unchanged.
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
        target_atr_mult: float = 2.50,
        target_contracts: int = 1,
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
        self.target_atr_mult = target_atr_mult
        self.target_contracts = max(0, min(int(target_contracts), int(size)))

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

        if ctx.position:
            self._manage_open_position(ctx)
            return

        if ctx.working_order_ids:
            return

        if self.slow not in ctx.bars:
            return

        history = ctx.history(self.slow)
        if len(history) < self.lookback + 2:
            return

        window = history.last(self.lookback + 2)
        if len(window) < self.lookback + 2:
            return

        last = window[-1]

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

    def _cancel_working_exits(self, ctx) -> None:
        for tag in ("protective-stop", "take-profit"):
            for order in ctx.find_orders(tag=tag):
                ctx.cancel(order.id)

    def _resize_stop(self, ctx, remaining: int) -> None:
        stops = ctx.find_orders(tag="protective-stop")
        if not stops:
            return
        stop_order = stops[0]
        if stop_order.size != remaining:
            ctx.modify(stop_order.id, size=remaining)

    def _place_exit_orders(self, ctx, fill) -> None:
        entry_atr = self.entry_atr
        if entry_atr is None or entry_atr <= 0:
            return

        pos = fill.position
        stop_distance = entry_atr * self.stop_mult
        if stop_distance <= 0:
            return

        existing_stops = ctx.find_orders(tag="protective-stop")
        if not existing_stops:
            if pos > 0:
                ctx.sell(
                    size=abs(pos),
                    type="stop",
                    trigger=fill.price - stop_distance,
                    tif="GTC",
                    reduce_only=True,
                    tag="protective-stop",
                )
            else:
                ctx.buy(
                    size=abs(pos),
                    type="stop",
                    trigger=fill.price + stop_distance,
                    tif="GTC",
                    reduce_only=True,
                    tag="protective-stop",
                )
        else:
            stop_order = existing_stops[0]
            if stop_order.size != abs(pos):
                ctx.modify(stop_order.id, size=abs(pos))

        if self.target_contracts <= 0:
            return

        existing_targets = ctx.find_orders(tag="take-profit")
        if existing_targets:
            return

        target_distance = entry_atr * self.target_atr_mult
        if target_distance <= 0:
            return

        contracts = min(self.target_contracts, abs(pos))
        if contracts <= 0:
            return

        if pos > 0:
            ctx.sell(
                size=contracts,
                type="limit",
                limit=fill.price + target_distance,
                tif="GTC",
                reduce_only=True,
                tag="take-profit",
            )
        else:
            ctx.buy(
                size=contracts,
                type="limit",
                limit=fill.price - target_distance,
                tif="GTC",
                reduce_only=True,
                tag="take-profit",
            )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        previous_position = self._last_position
        self._last_position = fill.position

        if fill.position == 0:
            self._cancel_working_exits(ctx)
            self.entry_price = None
            self.entry_atr = None
            self.pending_atr = None
            return

        if previous_position == 0:
            if self.entry_price is None:
                self.entry_price = fill.price
                self.entry_atr = self.pending_atr
            self._place_exit_orders(ctx, fill)
            return

        if abs(fill.position) < abs(previous_position):
            self._resize_stop(ctx, abs(fill.position))
            return

        if abs(fill.position) > abs(previous_position):
            if self.entry_price is None:
                self.entry_price = fill.price
                self.entry_atr = self.pending_atr
            self._place_exit_orders(ctx, fill)

    def on_lifecycle(self, ctx) -> None:
        pass
