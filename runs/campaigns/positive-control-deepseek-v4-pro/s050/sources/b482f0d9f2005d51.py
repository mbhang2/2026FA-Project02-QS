"""Proposal: #61 plus a larger order size and robust partial-fill stop resizing.

The best prior strategy (#61) enters on a 5m breakout/trend + delta confirmation,
uses an ATR-sized protective stop and a 1m cadence chandelier/breakeven trail,
and trades size=3.  That strategy scored +58.7881.

This proposal keeps the same entry signal, ATR calculation and management
cadence exactly.  Two changes:

1.  `size` is raised from 3 to 4.  With a positive edge, larger size should
    scale the same trade decisions to a larger return.  The entry is a market
    order, so size=4 may produce more partial fills under the `unfavorable`
    fill model; this is expected.

2.  `on_fill` now handles partial exits explicitly.  If the engine fills only
    part of the protective stop, the remaining reduce-only stop is resized to
    the new position so it can never request more than is open.  The original
    #61 already worked, but this makes size=4 safer.

No other logic is changed; this is intentionally a small, low-risk step from
the best known working strategy.
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
        size: int = 4,
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
            self._last_position = 0
            return

        prev_abs = abs(previous_position)
        new_abs = abs(fill.position)

        # A partial protective-stop fill leaves a smaller position.  Resize the
        # remaining reduce-only stop to match so it can never over-request size.
        if new_abs < prev_abs:
            existing = ctx.find_orders(tag="protective-stop")
            if existing:
                stop_order = existing[0]
                if stop_order.size != new_abs:
                    ctx.modify(stop_order.id, size=new_abs)
            return

        # A non-increasing fill (e.g. a same-size event we do not act on).
        if new_abs <= prev_abs:
            return

        # Increasing position: either a new entry or a later partial entry fill.
        # We keep the first-fill price as the reference, which is what #61 did.
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
                    size=new_abs,
                    type="stop",
                    trigger=fill.price - distance,
                    tif="GTC",
                    reduce_only=True,
                    tag="protective-stop",
                )
            elif fill.position < 0:
                ctx.buy(
                    size=new_abs,
                    type="stop",
                    trigger=fill.price + distance,
                    tif="GTC",
                    reduce_only=True,
                    tag="protective-stop",
                )
        else:
            stop_order = existing[0]
            if stop_order.size != new_abs:
                ctx.modify(stop_order.id, size=new_abs)

    def on_lifecycle(self, ctx) -> None:
        pass
