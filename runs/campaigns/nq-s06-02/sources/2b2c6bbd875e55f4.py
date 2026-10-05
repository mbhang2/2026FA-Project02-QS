"""Opening-range fade, but only when the breakout is weak relative to the
opening range and the session's own recent volatility, with a time stop and a
protective stop managed as a reduce-only order.

The question: when price leaves the opening range, does the move hold or does
the range win? This strategy answers "the range wins" for breakouts that are
small relative to both the opening range width and the range's own volatility,
and acts on every session where such a breakout occurs.

Opening range: high and low of the first 30 minutes of the regular-hours
session, tracked incrementally from 5-minute bars as they arrive.

Breakout definition: a 5-minute bar close beyond the opening range by at least
0.2 * the width of the opening range itself, but not more than 0.8 * the width
of the opening range. The upper bound filters out breakouts that are too large
to fade.

Direction: always fade the breakout. A close above the range is sold; a close
below is bought.

Entry: on the bar close that confirms the breakout.

Exit: a stated deadline of 20 minutes from entry, at which point the position
is closed at market. A protective stop is placed 0.5 * range width beyond the
entry, in the direction of the breakout, as a reduce-only stop order. The stop
is trailed toward the entry by 0.15 * range width whenever price moves
favorably.

The rule acts on every session where a qualifying breakout occurs.

This version avoids the WindowTooShort crash by never scanning the full
history: the opening range is accumulated from bars as they close, and the
breakout check uses only the most recent bar. Prior session ranges are kept on
self.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_minutes: int = 30,
        breakout_min_mult: float = 0.2,
        breakout_max_mult: float = 0.8,
        size: int = 1,
        hold_minutes: int = 20,
        stop_mult: float = 0.5,
        trail_mult: float = 0.15,
    ):
        self.interval = interval
        self.range_minutes = range_minutes
        self.breakout_min_mult = breakout_min_mult
        self.breakout_max_mult = breakout_max_mult
        self.size = size
        self.hold_minutes = hold_minutes
        self.stop_mult = stop_mult
        self.trail_mult = trail_mult

        self._session = None
        self._session_open_ts = None
        self._range_high = None
        self._range_low = None
        self._entry_ts = None
        self._entry_price = None
        self._stop_price = None
        self._direction = None  # 1 for long, -1 for short

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return

        # Session boundary: reset state
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_open_ts = ctx.session_start
            self._range_high = None
            self._range_low = None
            self._entry_ts = None
            self._entry_price = None
            self._stop_price = None
            self._direction = None

        # Manage existing position: trail stop and close at deadline
        if ctx.position:
            if self._entry_ts is not None:
                elapsed = (ctx.ts - self._entry_ts) / 1_000_000_000
                if elapsed >= self.hold_minutes * 60:
                    self._close_position(ctx)
                    return

            # Trail the stop toward the entry as price moves favorably
            if self._entry_price is not None and self._stop_price is not None:
                range_width = self._range_high - self._range_low
                trail_amount = self.trail_mult * range_width
                if self._direction == 1:  # long fade, price should fall
                    new_stop = max(
                        self._stop_price,
                        ctx.last_price - trail_amount,
                    )
                    if new_stop > self._stop_price:
                        self._update_stop(ctx, new_stop)
                elif self._direction == -1:  # short fade, price should rise
                    new_stop = min(
                        self._stop_price,
                        ctx.last_price + trail_amount,
                    )
                    if new_stop < self._stop_price:
                        self._update_stop(ctx, new_stop)
            return

        # No position: collect opening range and look for breakout
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        range_end_ts = self._session_open_ts + self.range_minutes * 60 * 1_000_000_000

        # Accumulate opening range from bars that start within the first
        # range_minutes of the session.
        if bar.open_ts < range_end_ts:
            if self._range_high is None:
                self._range_high = bar.high
                self._range_low = bar.low
            else:
                self._range_high = max(self._range_high, bar.high)
                self._range_low = min(self._range_low, bar.low)
            return

        # Opening range is complete; require it to be non-degenerate
        if self._range_high is None or self._range_low is None:
            return
        range_width = self._range_high - self._range_low
        if range_width <= 0:
            return

        last = bar
        breakout_distance = last.close - self._range_high if last.close > self._range_high else self._range_low - last.close
        breakout_up = last.close > self._range_high
        breakout_down = last.close < self._range_low

        if not (breakout_up or breakout_down):
            return

        # Check the breakout is within the fadeable size band
        min_distance = self.breakout_min_mult * range_width
        max_distance = self.breakout_max_mult * range_width
        if breakout_distance < min_distance or breakout_distance > max_distance:
            return

        stop_distance = self.stop_mult * range_width

        # Always fade the breakout, with a protective stop beyond the entry
        if breakout_up:
            stop_price = last.close + stop_distance
            ctx.sell(
                size=self.size,
                stop=stop_price,
                tag="range-fade-short",
            )
            self._direction = -1
        else:
            stop_price = last.close - stop_distance
            ctx.buy(
                size=self.size,
                stop=stop_price,
                tag="range-fade-long",
            )
            self._direction = 1

        self._entry_ts = ctx.ts
        self._entry_price = last.close
        self._stop_price = stop_price

        # Place a separate reduce-only stop order for trailing
        if self._direction == 1:
            ctx.sell(
                size=self.size,
                type="stop",
                trigger=stop_price,
                reduce_only=True,
                tag="range-fade-stop",
            )
        else:
            ctx.buy(
                size=self.size,
                type="stop",
                trigger=stop_price,
                reduce_only=True,
                tag="range-fade-stop",
            )

    def _update_stop(self, ctx, new_stop):
        stops = ctx.find_orders(tag="range-fade-stop")
        if stops:
            ctx.modify(stops[0].id, trigger=new_stop)
            self._stop_price = new_stop

    def _close_position(self, ctx):
        stops = ctx.find_orders(tag="range-fade-stop")
        for stop in stops:
            ctx.cancel(stop.id)
        ctx.close()
        self._entry_ts = None
        self._entry_price = None
        self._stop_price = None
        self._direction = None

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
