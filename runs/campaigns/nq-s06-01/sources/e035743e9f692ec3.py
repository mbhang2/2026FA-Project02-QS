"""Opening-range breakout with prior-session bias, entering on strength.

This variant builds on #79 (+0.3953), the best so far, but changes the
entry trigger to use a stop order at a buffer beyond the 1m bar's close,
rather than a limit order waiting for a pullback. The prior-session bias
filter and opening range are kept.

Changes from #79:
- Replace the limit order with a stop order triggered at the breakout
  close plus a small buffer, to enter on continuation rather than
  pullback.
- Increase patience bars to allow the stop to trigger.
- Keep all other parameters identical to #79.
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
        min_strength_mult: float = 0.15,
        trigger_buffer_mult: float = 0.1,
        patience_bars: int = 12,
    ):
        self.interval = interval
        self.fast_interval = fast_interval
        self.daily_interval = daily_interval
        self.opening_minutes = opening_minutes
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_strength_mult = min_strength_mult
        self.trigger_buffer_mult = trigger_buffer_mult
        self.patience_bars = patience_bars

        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._opening_range = None
        self._session_bars = 0
        self._breakout_direction = None
        self._trigger_price = None
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
            self._trigger_price = None
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
            self._trigger_price = None
            self._entry_tag = None
            self._bars_since_breakout = 0

        if self._opening_range is None or self._opening_range <= 0:
            return

        if ctx.position:
            return

        if ctx.working_order_ids:
            self._bars_since_breakout += 1
            if self._bars_since_breakout >= self.patience_bars:
                ctx.cancel_all()
                self._breakout_direction = None
                self._trigger_price = None
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

            strength_threshold = self.min_strength_mult * self._opening_range
            buffer = self.trigger_buffer_mult * self._opening_range

            if bar.close > self._opening_high and bias > 0:
                if bar.close - self._opening_high < strength_threshold:
                    return
                self._breakout_direction = "long"
                self._trigger_price = bar.close + buffer
                self._entry_tag = "momentum-long"
                self._bars_since_breakout = 0
            elif bar.close < self._opening_low and bias < 0:
                if self._opening_low - bar.close < strength_threshold:
                    return
                self._breakout_direction = "short"
                self._trigger_price = bar.close - buffer
                self._entry_tag = "momentum-short"
                self._bars_since_breakout = 0
            else:
                return

        if self._breakout_direction == "long":
            if ctx.last_price < self._trigger_price:
                ctx.buy(
                    size=self.size,
                    type="stop",
                    trigger=self._trigger_price,
                    stop_offset=self.stop_mult * self._opening_range,
                    target_offset=self.target_mult * self._opening_range,
                    tag=self._entry_tag,
                )
        else:
            if ctx.last_price > self._trigger_price:
                ctx.sell(
                    size=self.size,
                    type="stop",
                    trigger=self._trigger_price,
                    stop_offset=self.stop_mult * self._opening_range,
                    target_offset=self.target_mult * self._opening_range,
                    tag=self._entry_tag,
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
