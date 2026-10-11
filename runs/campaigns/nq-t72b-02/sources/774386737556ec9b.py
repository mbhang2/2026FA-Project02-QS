"""Test whether an outsized move within a session continues.

Hypothesis: after a bar whose absolute return is large relative to the
session's typical bar move so far, price continues in the same direction.
We measure "typical" as the average absolute close-open return of prior bars
in the same session. When the current bar's absolute return exceeds a
multiple of that average, we enter in the direction of the bar, with a
bracket for protection. This is a direct test of continuation after outsized
moves, as opposed to the give-back (mean reversion) alternative.
"""

from __future__ import annotations


class Strategy:
    """Continuation after outsized intra-session moves."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        min_bars: int = 5,
        threshold: float = 2.0,
        stop_mult: float = 0.5,
        target_mult: float = 1.0,
        size: int = 1,
    ):
        self.interval = interval
        self.min_bars = min_bars          # bars needed before we can compute baseline
        self.threshold = threshold        # how many times the average move counts as outsized
        self.stop_mult = stop_mult        # stop distance as fraction of the move
        self.target_mult = target_mult    # target distance as fraction of the move
        self.size = size

        # Per-session state
        self.current_session: int | None = None
        self.session_bars: list[tuple[float, float]] = []  # list of (close, open) for bars in this session

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Detect new session and reset state
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            self.session_bars = []

        # Append the just-closed bar
        bar = ctx.history(self.interval)[-1]
        self.session_bars.append((bar.close, bar.open))

        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        # Need enough prior bars to establish a baseline
        if len(self.session_bars) < self.min_bars + 1:
            return

        # Baseline: average absolute return of all bars before the current one
        prior_bars = self.session_bars[:-1]
        avg_abs_ret = sum(abs(c - o) for c, o in prior_bars) / len(prior_bars)
        if avg_abs_ret <= 0:
            return

        # Current bar's move
        current_close, current_open = self.session_bars[-1]
        current_abs_ret = abs(current_close - current_open)

        # Is it outsized?
        if current_abs_ret <= self.threshold * avg_abs_ret:
            return

        # Direction of the move
        direction = 1 if current_close > current_open else -1

        # Bracket distances proportional to the move, with a floor
        stop_dist = max(0.25, self.stop_mult * current_abs_ret)
        target_dist = max(0.25, self.target_mult * current_abs_ret)

        if direction > 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="outsized-continuation-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="outsized-continuation-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
