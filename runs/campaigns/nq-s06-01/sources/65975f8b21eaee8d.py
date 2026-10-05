"""Opening-range breakout with prior-session bias, entering on a pullback
that retraces a fixed fraction of the breakout bar's range.

This variant simplifies the entry: instead of waiting for a pullback limit
order, it enters immediately on the breakout bar close with a market order.
The stop and target are placed as offsets from the entry price based on the
opening range. The idea is to capture the breakout move without the
uncertainty of whether a pullback will occur.

Changes from #39 (recent, +0.1575):
- Removed the pullback limit order entirely; enter at market on the close
  of the breakout bar.
- This eliminates unfilled limit orders and the associated state management.
- Stop and target offsets are now applied to the market entry price, so the
  bracket is armed at the fill.
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
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_minutes = opening_minutes
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._opening_range = None
        self._session_bars = 0

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
            return

        if self._opening_range is None or self._opening_range <= 0:
            return

        if ctx.position or ctx.working_order_ids:
            return

        daily = ctx.history(self.daily_interval)
        if len(daily) < 2:
            return
        prev = daily[-2]
        bias = prev.close - prev.open
        if bias == 0:
            return

        if bar.close > self._opening_high and bias > 0:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_mult * self._opening_range,
                target_offset=self.target_mult * self._opening_range,
                tag="breakout-long",
            )
        elif bar.close < self._opening_low and bias < 0:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_mult * self._opening_range,
                target_offset=self.target_mult * self._opening_range,
                tag="breakout-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
