"""CVD continuation on the BTC perpetual.

The claim under test: when aggressive buying outweighs aggressive selling,
price continues higher for a while; when selling outweighs buying, it continues
lower. I measure flow as cumulative volume delta over the last `lookback`
one-hour bars, normalised by total volume over the same bars, so the signal is a
fraction of traded volume rather than a raw contract count. "Large" is relative
to the recent distribution of that same statistic: the strategy keeps a rolling
window of past ratios and requires the current ratio to be above its 80th
percentile (long) or below its 20th (short), with an absolute floor so a quiet
market is not traded.

A position is held for a fixed `hold_bars` horizon, the same length as the
measurement window, and then closed at market. A cooldown after an exit stops a
persistent signal from churning straight back in. No brackets are used: the test
is the horizon, and a bracket would change it. Only bar data is consumed, so
there is no tick subscription and no order-flow reconstruction beyond
`bar.delta`, which every time bar carries.

The strategy trades one position at a time, uses market orders (the size is far
inside the top of book), and never stores an order id across callbacks.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "1h",
        lookback: int = 6,
        hold_bars: int = 6,
        ratio_window: int = 240,
        min_history: int = 120,
        high_q: float = 0.80,
        low_q: float = 0.20,
        min_abs: float = 0.08,
        cooldown_bars: int = 6,
        size: int = 1000,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.hold_bars = hold_bars
        self.ratio_window = ratio_window
        self.min_history = min_history
        self.high_q = high_q
        self.low_q = low_q
        self.min_abs = min_abs
        self.cooldown_bars = cooldown_bars
        self.size = size

        self.ratios: deque[float] = deque(maxlen=ratio_window)
        self.bars_held = 0
        self.cooldown = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        history = ctx.history(self.interval)
        window = history.last(self.lookback)
        if len(window) < self.lookback:
            return

        total_volume = sum(bar.volume for bar in window)
        if total_volume <= 0:
            return
        delta = sum(bar.delta for bar in window)
        ratio = float(delta) / float(total_volume)

        if ctx.position:
            self.bars_held += 1
            if (
                ctx.actionable
                and self.bars_held >= self.hold_bars
                and not ctx.working_order_ids
            ):
                ctx.close()
            self.ratios.append(ratio)
            return

        if self.cooldown > 0:
            self.cooldown -= 1

        if not ctx.actionable:
            self.ratios.append(ratio)
            return
        if ctx.working_order_ids:
            self.ratios.append(ratio)
            return
        if self.cooldown > 0:
            self.ratios.append(ratio)
            return
        if len(self.ratios) < self.min_history:
            self.ratios.append(ratio)
            return

        ordered = sorted(self.ratios)
        n = len(ordered)
        hi = ordered[int(self.high_q * (n - 1))]
        lo = ordered[int(self.low_q * (n - 1))]

        if ratio > max(hi, self.min_abs):
            ctx.buy(size=self.size, tag="cvd-long")
        elif ratio < min(lo, -self.min_abs):
            ctx.sell(size=self.size, tag="cvd-short")

        self.ratios.append(ratio)

    def on_fill(self, ctx) -> None:
        if ctx.fill.position == 0:
            self.bars_held = 0
            self.cooldown = self.cooldown_bars
        else:
            self.bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        pass
