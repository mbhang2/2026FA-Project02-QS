"""Test whether an outsized counter-trend move within a session gives back.

Hypothesis: after a bar whose absolute close-open return is large relative to
the recent typical bar move, and whose direction is opposite to the session's
prevailing trend, price tends to revert toward the trend. We measure "typical"
as the average absolute close-open return of the last N bars in the same
session. When the current bar's absolute return exceeds a multiple of that
average and the bar moves against the session direction, we enter in the
direction of the session trend (fading the counter-trend move), with a bracket
for protection.

This is the give-back counterpart to the prior continuation strategies. We
limit to one trade per session and require a minimum time remaining to avoid
holding into the close. We also flatten any open position near the session
close to avoid overnight risk.
"""

from __future__ import annotations


class Strategy:
    """Mean reversion after outsized counter-trend intra-session moves."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        min_bars: int = 20,
        threshold: float = 2.0,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        size: int = 1,
        min_abs_move: float = 0.5,
        min_time_left_ns: int = 30 * 60 * 1_000_000_000,  # 30 minutes
        close_margin_ns: int = 5 * 60 * 1_000_000_000,    # 5 minutes before close
    ):
        self.interval = interval
        self.min_bars = min_bars
        self.threshold = threshold
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.min_abs_move = min_abs_move
        self.min_time_left_ns = min_time_left_ns
        self.close_margin_ns = close_margin_ns

        # Per-session state
        self.current_session: int | None = None
        self.session_open: float | None = None
        self.session_bars: list[tuple[float, float]] = []  # (close, open)
        self.traded_this_session: bool = False
        self.closing_this_session: bool = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Detect new session and reset state
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            self.session_open = None
            self.session_bars = []
            self.traded_this_session = False
            self.closing_this_session = False

        # Append the just-closed bar
        history = ctx.history(self.interval)
        if not history:
            return
        bar = history[-1]
        if self.session_open is None:
            self.session_open = bar.open
        self.session_bars.append((bar.close, bar.open))

        if not ctx.actionable:
            return

        # Flatten near session close if we have a position
        if ctx.position and not self.closing_this_session:
            if ctx.session_end - ctx.ts < self.close_margin_ns:
                ctx.close(tag="session-close")
                self.closing_this_session = True
                return

        if ctx.position or ctx.working_order_ids:
            return
        if self.traded_this_session:
            return

        # Need enough prior bars to establish a baseline
        if len(self.session_bars) < self.min_bars + 1:
            return

        # Check time remaining in session
        if ctx.session_end - ctx.ts < self.min_time_left_ns:
            return

        # Baseline: average absolute return of the last min_bars bars before current
        prior_bars = self.session_bars[-(self.min_bars + 1):-1]
        avg_abs_ret = sum(abs(c - o) for c, o in prior_bars) / len(prior_bars)
        if avg_abs_ret <= 0:
            return

        # Current bar's move
        current_close, current_open = self.session_bars[-1]
        current_abs_ret = abs(current_close - current_open)

        # Is it outsized?
        if current_abs_ret <= self.threshold * avg_abs_ret:
            return
        if current_abs_ret < self.min_abs_move:
            return

        # Session direction based on current price vs session open
        if current_close == self.session_open:
            return
        session_direction = 1 if current_close > self.session_open else -1

        # Bar direction
        bar_direction = 1 if current_close > current_open else -1

        # Only trade counter-trend moves (bar direction opposite to session direction)
        if bar_direction == session_direction:
            return

        # Bracket distances proportional to the move, with a floor
        stop_dist = max(0.25, self.stop_mult * current_abs_ret)
        target_dist = max(0.25, self.target_mult * current_abs_ret)

        # Enter in the direction of the session trend (fading the counter-trend move)
        if session_direction > 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="give-back-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="give-back-short",
            )

        self.traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
