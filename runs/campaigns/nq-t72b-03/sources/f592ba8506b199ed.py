"""Investigate whether an outsized move inside a session gives back, with
limit entry on a retracement.

Refinement of prior give-back strategy. Instead of entering at market on the
bar close, we place a limit order opposite to the outsized move, expecting a
retracement. This improves entry price and filters out moves that do not
retrace. We measure "outsized" relative to the session's high-low range so far
(excluding the current bar), which adapts to the session's volatility. We
require a minimum number of bars to establish a baseline and a minimum time
left in the session. The limit order is cancelled if not filled within a few
bars.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        min_bars: int = 20,
        range_mult: float = 2.0,
        retrace_frac: float = 0.3,
        stop_mult: float = 0.5,
        target_mult: float = 1.5,
        patience_bars: int = 3,
        size: int = 1,
        min_time_left_ns: int = 30 * 60 * 1_000_000_000,  # 30 minutes
    ):
        self.interval = interval
        self.min_bars = min_bars
        self.range_mult = range_mult
        self.retrace_frac = retrace_frac
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.patience_bars = patience_bars
        self.size = size
        self.min_time_left_ns = min_time_left_ns

        # Session state
        self._session: int | None = None
        self._bars_this_session: int = 0
        self._session_high: float | None = None
        self._session_low: float | None = None
        self._entry_bar_count: int | None = None  # bar index when entry limit was placed

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Reset session state on session change
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._session_high = None
            self._session_low = None
            self._entry_bar_count = None

        # Only act on our interval's bar close
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]
        self._bars_this_session += 1

        # If we have a working entry order, check if it's too old and cancel
        if self._entry_bar_count is not None:
            entry_orders = ctx.find_orders(tag="entry")
            if entry_orders:
                age = self._bars_this_session - self._entry_bar_count
                if age > self.patience_bars:
                    ctx.cancel(entry_orders[0].id)
                    self._entry_bar_count = None
                return  # do not place new order while one is working
            else:
                # Order no longer working (filled, cancelled, expired)
                self._entry_bar_count = None

        # If we have a position, nothing to do (bracket manages exit)
        if ctx.position:
            return

        # Check actionable and time left
        if not ctx.actionable:
            return
        time_left = ctx.session_end - ctx.ts
        if time_left < self.min_time_left_ns:
            return

        # Need enough bars to have a meaningful session range
        if self._bars_this_session < self.min_bars:
            return

        # Compute session range so far (excluding current bar)
        if self._session_high is None or self._session_low is None:
            session_range = 0.0
        else:
            session_range = self._session_high - self._session_low
        if session_range <= 0:
            return

        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return

        # Outsized move if current bar range exceeds multiple of session range
        if bar_range < self.range_mult * session_range:
            return

        # Direction: give-back (close > open -> sell, close < open -> buy)
        if bar.close == bar.open:
            return

        # Compute limit price for retracement entry
        if bar.close > bar.open:
            # Outsized up bar -> expect give-back, place sell limit above close
            limit_price = bar.close + self.retrace_frac * bar_range
            stop_dist = self.stop_mult * bar_range
            target_dist = self.target_mult * bar_range
            # For short: stop above entry, target below entry
            stop_price = limit_price + stop_dist
            target_price = limit_price - target_dist
            ctx.sell(
                size=self.size,
                type="limit",
                limit=limit_price,
                stop=stop_price,
                target=target_price,
                tag="entry",
            )
        else:
            # Outsized down bar -> expect give-back, place buy limit below close
            limit_price = bar.close - self.retrace_frac * bar_range
            stop_dist = self.stop_mult * bar_range
            target_dist = self.target_mult * bar_range
            # For long: stop below entry, target above entry
            stop_price = limit_price - stop_dist
            target_price = limit_price + target_dist
            ctx.buy(
                size=self.size,
                type="limit",
                limit=limit_price,
                stop=stop_price,
                target=target_price,
                tag="entry",
            )

        # Record bar index when entry order was placed
        self._entry_bar_count = self._bars_this_session

        # Update session high/low after signal check (so current bar not in denominator)
        if self._session_high is None or bar.high > self._session_high:
            self._session_high = bar.high
        if self._session_low is None or bar.low < self._session_low:
            self._session_low = bar.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
