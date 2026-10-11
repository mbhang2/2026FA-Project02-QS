"""Investigate whether an outsized intra-session move tends to continue or give back.

This strategy refines the continuation hypothesis by requiring not just a single
outsized bar followed by a breakout, but a **cluster** of outsized movement.
Specifically, we look for two consecutive bars where the combined range is
unusually large relative to the rolling ATR, and then require the current bar to
close beyond the cluster's extreme in the direction of the move.

The rationale: a single outsized bar may be noise or a one-off liquidity event,
but two consecutive large bars suggest persistent order flow and a higher
probability of continuation. By requiring the breakout to exceed the cluster's
extreme, we filter for genuine momentum rather than a brief spike.

The mechanism is claimed to operate throughout the session, independent of hour,
month, or weekday.

Key design choices:
- Rolling ATR (default 20 bars) as the volatility measure.
- Cluster range: sum of the ranges of the last two bars must exceed
  cluster_mult * ATR (default 3.0).
- Direction is determined by the net close-to-close movement over the two bars.
- Continuation confirmation: current bar close must exceed the cluster's high
  (for up moves) or fall below the cluster's low (for down moves) by at least
  continuation_frac * cluster_range.
- Bracket exits use ATR-scaled offsets.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of outsized two-bar clusters with rolling ATR."""

    def __init__(self, *, interval: str = "5m", atr_window: int = 20,
                 cluster_mult: float = 3.0, continuation_frac: float = 0.15,
                 stop_atr: float = 1.0, target_atr: float = 2.0,
                 size: int = 1):
        self.interval = interval
        self.atr_window = atr_window
        self.cluster_mult = cluster_mult
        self.continuation_frac = continuation_frac
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.size = size

        # Per-session state.
        self._session = None
        self._trs: list[float] = []
        self._prev_close = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset state when the session changes.
        if ctx.session != self._session:
            self._session = ctx.session
            self._trs = []
            self._prev_close = None

        history = ctx.history(self.interval)
        if len(history) < 4:
            return

        bar = history[-1]

        # Update true range series.
        if self._prev_close is None:
            self._prev_close = bar.open
        tr = max(bar.high - bar.low,
                 abs(bar.high - self._prev_close),
                 abs(bar.low - self._prev_close))
        self._trs.append(tr)
        self._prev_close = bar.close

        # Need at least a few bars for a meaningful ATR.
        if len(self._trs) < 3:
            return

        # Rolling ATR over the most recent bars.
        window = self._trs[-self.atr_window:]
        atr = sum(window) / len(window)
        if atr <= 0:
            return

        # One position at a time; the bracket handles exits.
        if ctx.position or ctx.working_order_ids:
            return

        # The cluster is the last two closed bars (history[-2] and history[-3]).
        bar1 = history[-3]
        bar2 = history[-2]
        cluster_range = (bar1.high - bar1.low) + (bar2.high - bar2.low)
        if cluster_range < self.cluster_mult * atr:
            return

        # Direction: net close-to-close movement over the two bars.
        net_move = bar2.close - bar1.open
        if net_move == 0:
            return
        direction = 1 if net_move > 0 else -1

        # Cluster extreme: highest high and lowest low over the two bars.
        cluster_high = max(bar1.high, bar2.high)
        cluster_low = min(bar1.low, bar2.low)

        # Continuation requires the current bar to break the cluster extreme
        # by at least continuation_frac * cluster_range.
        if direction > 0:
            required_break = cluster_high + self.continuation_frac * cluster_range
            if bar.close <= required_break:
                return
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_atr * atr,
                target_offset=self.target_atr * atr,
                tag="cluster-continuation-long",
            )
        else:
            required_break = cluster_low - self.continuation_frac * cluster_range
            if bar.close >= required_break:
                return
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_atr * atr,
                target_offset=self.target_atr * atr,
                tag="cluster-continuation-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
