"""Opening-range breakout with prior-session bias, entering on a pullback
that retraces a fixed fraction of the breakout bar's range, with a
time-based cancellation. This variant adds a volatility filter but
relaxes it to avoid being too restrictive, and adjusts the pullback
fraction based on the opening range relative to the prior session.

The campaign question: when price leaves the opening range, does the move hold
or does the range win? This strategy answers "the move holds" in the direction
of the prior session's bias, on the ordinary run of sessions.

Changes from #19 (best so far, +0.2149):
- Add a minimum opening range filter, but only require 0.3 times the prior
  session's range instead of 0.5, to avoid filtering too many sessions.
- Add a maximum breakout bar range filter at 3.0 times the opening range
  instead of 2.0, to avoid rejecting genuine breakouts.
- Adjust the pullback fraction: if the opening range is small relative to
  the prior session, use a larger pullback fraction (0.6) to enter on a
  deeper retracement; otherwise use 0.5.
- Keep the bracket offsets based on the opening range.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        daily_interval: str = "1d",
        opening_minutes: int = 30,
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 1.5,
        pullback_mult: float = 0.5,
        pullback_mult_small_range: float = 0.6,
        patience_bars: int = 3,
        min_opening_range_mult: float = 0.3,
        max_breakout_range_mult: float = 3.0,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_minutes = opening_minutes
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.pullback_mult = pullback_mult
        self.pullback_mult_small_range = pullback_mult_small_range
        self.patience_bars = patience_bars
        self.min_opening_range_mult = min_opening_range_mult
        self.max_breakout_range_mult = max_breakout_range_mult

        # State per session
        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._opening_range = None
        self._session_bars = 0
        self._breakout_direction = None
        self._limit_price = None
        self._entry_tag = None
        self._bars_since_breakout = 0
        self._prior_session_range = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.daily_interval)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return

        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._opening_high = None
            self._opening_low = None
            self._opening_range = None
            self._session_bars = 0
            self._breakout_direction = None
            self._limit_price = None
            self._entry_tag = None
            self._bars_since_breakout = 0
            self._prior_session_range = None

        self._session_bars += 1
        bar = ctx.bars[self.interval]

        opening_bars = self.opening_minutes // 5
        if self._session_bars <= opening_bars:
            if self._opening_high is None or bar.high > self._opening_high:
                self._opening_high = bar.high
            if self._opening_low is None or bar.low < self._opening_low:
                self._opening_low = bar.low
            if self._session_bars == opening_bars:
                if self._opening_high is not None and self._opening_low is not None:
                    self._opening_range = self._opening_high - self._opening_low
                    daily = ctx.history(self.daily_interval)
                    if len(daily) >= 2:
                        prev = daily[-2]
                        self._prior_session_range = prev.high - prev.low
            return

        if self._opening_range is None or self._opening_range <= 0:
            return

        if self._prior_session_range is not None and self._prior_session_range > 0:
            if self._opening_range < self.min_opening_range_mult * self._prior_session_range:
                return

        if ctx.position:
            return

        if ctx.working_order_ids:
            self._bars_since_breakout += 1
            if self._bars_since_breakout >= self.patience_bars:
                ctx.cancel_all()
                self._breakout_direction = None
                self._limit_price = None
                self._entry_tag = None
                self._bars_since_breakout = 0
            return

        if self._breakout_direction is None:
            daily = ctx.history(self.daily_interval)
            if len(daily) < 2:
                return
            prev = daily[-2]
            bias = prev.close - prev.open
            if bias == 0:
                return

            bar_range = bar.high - bar.low
            if bar_range <= 0:
                return

            if bar_range > self.max_breakout_range_mult * self._opening_range:
                return

            # Determine pullback fraction based on relative opening range size.
            if (
                self._prior_session_range is not None
                and self._prior_session_range > 0
                and self._opening_range < 0.5 * self._prior_session_range
            ):
                pullback = self.pullback_mult_small_range
            else:
                pullback = self.pullback_mult

            if bar.close > self._opening_high and bias > 0:
                self._breakout_direction = "long"
                self._limit_price = bar.close - pullback * bar_range
                self._entry_tag = "pullback-long"
                self._bars_since_breakout = 0
            elif bar.close < self._opening_low and bias < 0:
                self._breakout_direction = "short"
                self._limit_price = bar.close + pullback * bar_range
                self._entry_tag = "pullback-short"
                self._bars_since_breakout = 0
            else:
                return

        if self._breakout_direction == "long":
            if ctx.last_price > self._limit_price:
                ctx.buy(
                    size=self.size,
                    type="limit",
                    limit=self._limit_price,
                    stop_offset=self.stop_mult * self._opening_range,
                    target_offset=self.target_mult * self._opening_range,
                    tag=self._entry_tag,
                )
        else:
            if ctx.last_price < self._limit_price:
                ctx.sell(
                    size=self.size,
                    type="limit",
                    limit=self._limit_price,
                    stop_offset=self.stop_mult * self._opening_range,
                    target_offset=self.target_mult * self._opening_range,
                    tag=self._entry_tag,
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
