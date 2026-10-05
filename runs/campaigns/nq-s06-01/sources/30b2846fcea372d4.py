"""Opening-range breakout with prior-session bias, entering on a pullback
that retraces a fixed fraction of the breakout bar's range.

This variant builds on #54 (+0.3469), the best so far, but modifies the
entry logic to require a minimum breakout strength relative to the opening
range, and to skip sessions where the opening range is too small to trade.

Changes from #54:
- Add `min_range` parameter (default 2.0) to skip sessions where the
  opening range is too small.
- Add `min_breakout_strength` parameter (default 0.5) to require the
  breakout bar to close at least this fraction of the opening range beyond
  the range boundary.
- All other parameters and logic remain identical to #54.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        fast_interval: str = "1m",
        daily_interval: str = "1d",
        opening_minutes: int = 30,
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 1.5,
        pullback_mult: float = 0.5,
        bias_strength_mult: float = 0.25,
        max_pullback_mult: float = 0.5,
        min_strength_mult: float = 0.15,
        patience_bars: int = 6,
        min_range: float = 2.0,
        min_breakout_strength: float = 0.5,
    ):
        self.interval = interval
        self.fast_interval = fast_interval
        self.daily_interval = daily_interval
        self.opening_minutes = opening_minutes
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.pullback_mult = pullback_mult
        self.bias_strength_mult = bias_strength_mult
        self.max_pullback_mult = max_pullback_mult
        self.min_strength_mult = min_strength_mult
        self.patience_bars = patience_bars
        self.min_range = min_range
        self.min_breakout_strength = min_breakout_strength

        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._opening_range = None
        self._session_bars = 0
        self._breakout_direction = None
        self._limit_price = None
        self._entry_tag = None
        self._bars_since_breakout = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.daily_interval)

    def on_bars(self, ctx) -> None:
        if self.interval in ctx.bars:
            self._on_5m_bar(ctx)
        if self.fast_interval in ctx.bars:
            self._on_1m_bar(ctx)

    def _on_5m_bar(self, ctx) -> None:
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

    def _on_1m_bar(self, ctx) -> None:
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

        if self._opening_range is None or self._opening_range <= 0:
            return

        # Skip sessions where the opening range is too small.
        if self._opening_range < self.min_range:
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

        bar = ctx.bars[self.fast_interval]

        if self._breakout_direction is None:
            daily = ctx.history(self.daily_interval)
            if len(daily) < 2:
                return
            prev = daily[-2]
            bias = prev.close - prev.open
            if bias == 0:
                return

            abs_bias = abs(bias)
            adaptive_threshold = self.bias_strength_mult * abs_bias
            strength_threshold = max(
                adaptive_threshold, self.min_strength_mult * self._opening_range
            )
            max_pullback = self.max_pullback_mult * self._opening_range

            bar_range = bar.high - bar.low
            if bar_range <= 0:
                return

            # Minimum breakout strength: close must be at least
            # min_breakout_strength * opening_range beyond the boundary.
            min_breakout_distance = self.min_breakout_strength * self._opening_range

            if bar.close > self._opening_high and bias > 0:
                breakout_distance = bar.close - self._opening_high
                if breakout_distance < strength_threshold:
                    return
                if breakout_distance < min_breakout_distance:
                    return
                pullback_distance = self.pullback_mult * bar_range
                if pullback_distance > max_pullback:
                    return
                self._breakout_direction = "long"
                self._limit_price = bar.close - pullback_distance
                self._entry_tag = "pullback-long"
                self._bars_since_breakout = 0
            elif bar.close < self._opening_low and bias < 0:
                breakout_distance = self._opening_low - bar.close
                if breakout_distance < strength_threshold:
                    return
                if breakout_distance < min_breakout_distance:
                    return
                pullback_distance = self.pullback_mult * bar_range
                if pullback_distance > max_pullback:
                    return
                self._breakout_direction = "short"
                self._limit_price = bar.close + pullback_distance
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
