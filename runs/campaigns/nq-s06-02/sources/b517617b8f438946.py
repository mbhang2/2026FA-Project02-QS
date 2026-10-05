"""Opening-range fade with a volatility-scaled breakout threshold and a
bracket-managed exit, using a smaller threshold for more trades.

The question: when price leaves the opening range, does the move hold or does
the range win? This strategy answers "the range wins" for breakouts that are
large relative to recent realized volatility, and acts on every session where
such a breakout occurs.

Opening range: high and low of the first 30 minutes of the regular-hours
session, tracked incrementally from 5-minute bars as they arrive.

Breakout definition: a 5-minute bar close beyond the opening range by at least
0.2 * the average true range of the prior 10 sessions. This is a smaller
threshold than the previous version, producing more trades.

Direction: always fade the breakout. A close above the range is sold; a close
below is bought.

Entry: on the bar close that confirms the breakout.

Exit: a stated deadline of 10 minutes from entry, at which point the position
is closed at market. A protective stop is placed 0.2 average true ranges
beyond the entry, in the direction of the breakout. The stop is trailed toward
the entry as price moves favorably, locking in gains while still giving the
fade room to work.

The rule acts on every session where a qualifying breakout occurs.

This version avoids the WindowTooShort crash by never scanning the full
history: the opening range is accumulated from bars as they close, and the
breakout check uses only the most recent bar. Prior average true ranges are
kept on self.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_minutes: int = 30,
        breakout_mult: float = 0.2,
        size: int = 1,
        hold_minutes: int = 10,
        stop_mult: float = 0.2,
        lookback_sessions: int = 10,
        trail_mult: float = 0.1,
    ):
        self.interval = interval
        self.range_minutes = range_minutes
        self.breakout_mult = breakout_mult
        self.size = size
        self.hold_minutes = hold_minutes
        self.stop_mult = stop_mult
        self.lookback_sessions = lookback_sessions
        self.trail_mult = trail_mult

        self._session = None
        self._session_open_ts = None
        self._range_high = None
        self._range_low = None
        self._entry_ts = None
        self._prior_atrs: list[float] = []
        self._current_session_high = None
        self._current_session_low = None
        self._current_session_close = None
        self._entry_price = None
        self._stop_price = None
        self._direction = None  # 1 for long, -1 for short

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return

        # Session boundary: reset state and store the prior session's average
        # true range.
        if ctx.session != self._session:
            if self._session is not None:
                if (
                    self._current_session_high is not None
                    and self._current_session_low is not None
                    and self._current_session_close is not None
                ):
                    prev_close = self._current_session_close
                    true_range = max(
                        self._current_session_high - self._current_session_low,
                        abs(self._current_session_high - prev_close),
                        abs(self._current_session_low - prev_close),
                    )
                    self._prior_atrs.append(true_range)
                    if len(self._prior_atrs) > self.lookback_sessions:
                        self._prior_atrs.pop(0)

            self._session = ctx.session
            self._session_open_ts = ctx.session_start
            self._range_high = None
            self._range_low = None
            self._entry_ts = None
            self._current_session_high = None
            self._current_session_low = None
            self._current_session_close = None
            self._entry_price = None
            self._stop_price = None
            self._direction = None

        # Track the session's high, low, and close from all bars.
        for bar in ctx.closed:
            if self._current_session_high is None:
                self._current_session_high = bar.high
                self._current_session_low = bar.low
            else:
                self._current_session_high = max(self._current_session_high, bar.high)
                self._current_session_low = min(self._current_session_low, bar.low)
            self._current_session_close = bar.close

        # Manage existing position: trail stop and close at deadline
        if ctx.position:
            if self._entry_ts is not None:
                elapsed = (ctx.ts - self._entry_ts) / 1_000_000_000
                if elapsed >= self.hold_minutes * 60:
                    ctx.close()
                    self._entry_ts = None
                    self._entry_price = None
                    self._stop_price = None
                    self._direction = None
                    return

            # Trail the stop toward the entry as price moves favorably
            if self._entry_price is not None and self._stop_price is not None:
                if self._direction == 1:  # long fade, price should fall
                    new_stop = max(
                        self._stop_price,
                        ctx.last_price - self.trail_mult * (self._entry_price - ctx.last_price),
                    )
                    if new_stop > self._stop_price:
                        self._stop_price = new_stop
                        stops = ctx.find_orders(tag="range-fade-stop")
                        if stops:
                            ctx.modify(stops[0].id, trigger=self._stop_price)
                elif self._direction == -1:  # short fade, price should rise
                    new_stop = min(
                        self._stop_price,
                        ctx.last_price + self.trail_mult * (ctx.last_price - self._entry_price),
                    )
                    if new_stop < self._stop_price:
                        self._stop_price = new_stop
                        stops = ctx.find_orders(tag="range-fade-stop")
                        if stops:
                            ctx.modify(stops[0].id, trigger=self._stop_price)
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

        # Compute breakout threshold from recent average true ranges
        if len(self._prior_atrs) >= self.lookback_sessions:
            avg_atr = sum(self._prior_atrs) / len(self._prior_atrs)
            threshold = self.breakout_mult * avg_atr
            stop_distance = self.stop_mult * avg_atr
        else:
            threshold = self.breakout_mult * range_width
            stop_distance = self.stop_mult * range_width

        last = bar
        breakout_up = last.close > self._range_high + threshold
        breakout_down = last.close < self._range_low - threshold

        if not (breakout_up or breakout_down):
            return

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

        # Place a separate stop order for trailing
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

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
