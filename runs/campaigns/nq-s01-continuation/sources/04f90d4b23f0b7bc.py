"""Does an outsized move inside a session continue, or give it back?

Seventh proposal. Prior results:
- #1 session-range percentile continuation: +0.0874
- #2 give-back inversion of #1: +0.0047
- #4 rolling-window continuation: +0.0836
- #5 breakout-confirmed continuation: -0.0092

The best two (#1, #4) both measure an outsized move against recent range and
trade continuation. #5 added a breakout filter and a regime floor, which cut
exposure and hurt badly. This version returns to the core #1/#4 structure but
adds two refinements that should improve selectivity without destroying
exposure:

1. **Adaptive percentile instead of a fixed one.** #1 used a fixed 90th
   percentile of session ratios. Here the percentile is chosen per session
   based on the session's own volatility regime: quiet sessions get a lower
   percentile (more signals), busy sessions a higher one (fewer signals).
   This keeps exposure up on days where outsized moves are rare relative to
   range.

2. **Volatility-scaled bracket.** Instead of a constant multiplier on the
   session range, the stop and target are scaled by the ratio of the current
   bar's body to the average body of the last 20 bars. A truly outsized bar
   gets a wider bracket, letting continuation run; a marginal one gets a
   tighter bracket, cutting losses sooner.

The signal itself remains: a 5m bar whose body is large relative to the
session's realized range so far, entered in the direction of the bar.
"""

from __future__ import annotations

from src.core.types import Bar


class Strategy:
    """Continuation on outsized intra-session moves, adaptive percentile."""

    def __init__(self, *, interval: str = "5m", size: int = 1,
                 base_percentile: float = 0.85,
                 stop_mult: float = 1.0, target_mult: float = 1.5,
                 min_bars: int = 10, lookback: int = 20):
        self.interval = interval
        self.size = size
        self.base_percentile = base_percentile
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_bars = min_bars
        self.lookback = lookback
        self._session = None
        self._session_bars: list[Bar] = []
        self._session_ratios: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_bars = []
            self._session_ratios = []

        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        last = history[-1]
        if last.session != ctx.session:
            return
        self._session_bars.append(last)

        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        bars = self._session_bars
        if len(bars) < self.min_bars:
            return

        current = bars[-1]
        if current.close == current.open:
            return

        prior = bars[:-1]
        high = max(b.high for b in prior)
        low = min(b.low for b in prior)
        rng = high - low
        if rng <= 0:
            return

        move = abs(current.close - current.open)
        ratio = move / rng
        self._session_ratios.append(ratio)

        if len(self._session_ratios) < 5:
            return

        # Adaptive percentile based on recent volatility.
        recent_bodies = [abs(b.close - b.open) for b in bars[-self.lookback:]]
        avg_body = sum(recent_bodies) / len(recent_bodies)
        if avg_body <= 0:
            return

        # Quiet session -> lower percentile; busy session -> higher.
        vol_scale = min(max(avg_body / rng, 0.2), 2.0)
        percentile = self.base_percentile + 0.05 * (vol_scale - 1.0)
        percentile = min(max(percentile, 0.70), 0.95)

        sorted_ratios = sorted(self._session_ratios)
        idx = int(percentile * (len(sorted_ratios) - 1))
        threshold = sorted_ratios[idx]

        if ratio < threshold:
            return

        direction = 1 if current.close > current.open else -1

        # Volatility-scaled bracket: wider for more outsized bars.
        scale = move / avg_body
        scale = min(max(scale, 0.5), 3.0)
        stop = rng * self.stop_mult * scale
        target = rng * self.target_mult * scale

        if direction > 0:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="adaptive-continuation-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="adaptive-continuation-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
