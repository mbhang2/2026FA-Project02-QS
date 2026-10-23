"""A momentum strategy that builds on the previous best trial.

The prior strategy traded when the last closed 1m bar's body was large
relative to ATR and its signed delta did not disagree with the move. This
version keeps that engine and adds one further quality filter: the bar must
also close in the upper part of its own range for a long, or the lower part
for a short. That removes bars whose large body still closed near the
opposite extreme, which is a weaker momentum print than the body alone
suggests. Parameters stay close to the proven configuration.
"""

from __future__ import annotations


class Strategy:
    """Momentum with ATR-scaled threshold and close-location confirmation."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        lookback: int = 8,
        entry_frac: float = 0.20,
        stop_mult: float = 1.5,
        target_mult: float = 2.5,
        close_loc_min: float = 0.70,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.entry_frac = entry_frac
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.close_loc_min = close_loc_min
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        # Need lookback + 1 closed bars: prior window for ATR, last bar for signal.
        recent = ctx.history(self.interval).last(self.lookback + 1)
        if len(recent) < self.lookback + 1:
            return

        prior = recent[:-1]
        if len(prior) < 2:
            return

        trs = []
        for i in range(1, len(prior)):
            prev_close = prior[i - 1].close
            high = prior[i].high
            low = prior[i].low
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

        if not trs:
            return

        atr = sum(trs) / len(trs)
        if atr <= 0:
            return

        last = recent[-1]
        body = last.close - last.open
        rng = last.high - last.low

        # Close-location within the bar: bullish bars should close near the high,
        # bearish bars near the low. A zero-range bar is not tradeable anyway.
        if rng > 0:
            close_loc = (last.close - last.low) / rng
        else:
            close_loc = 0.5

        threshold = self.entry_frac * atr
        stop = self.stop_mult * atr
        target = self.target_mult * atr

        if (
            body >= threshold
            and last.delta >= 0
            and close_loc >= self.close_loc_min
        ):
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="momentum-long",
            )
        elif (
            body <= -threshold
            and last.delta <= 0
            and close_loc <= 1.0 - self.close_loc_min
        ):
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="momentum-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
