"""Test whether an outsized range move within a session continues.

Hypothesis: after a bar whose high-low range is large relative to the recent
typical range, and whose direction is the same as the session's prevailing
trend, price tends to continue in that direction. We measure "typical" as the
average range of the last N bars in the same session. When the current bar's
range exceeds a multiple of that average and the bar moves with the session
direction, we enter in the direction of the move, with a bracket for protection.

This is a variation of the best prior continuation strategy (#7), replacing the
close-open return with the full high-low range as the measure of the move. A
large range with a directional close may capture stronger momentum than a large
close-open return alone. We keep the same structure: one trade per session,
trend alignment, and a bracket. We also flatten near the session close.
"""

from __future__ import annotations


class Strategy:
    """Momentum after outsized trend-aligned range moves."""

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
        self.session_bars: list[tuple[float, float, float, float]] = []  # (open, high, low, close)
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
        self.session_bars.append((bar.open, bar.high, bar.low, bar.close))

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

        # Baseline: average range of the last min_bars bars before current
        prior_bars = self.session_bars[-(self.min_bars + 1):-1]
        avg_range = sum(b[2] - b[1] for b in prior_bars) / len(prior_bars)
        if avg_range <= 0:
            return

        # Current bar's data
        current_open, current_high, current_low, current_close = self.session_bars[-1]
        current_range = current_high - current_low

        # Is the range outsized?
        if current_range <= self.threshold * avg_range:
            return
        if current_range < self.min_abs_move:
            return

        # Session direction based on current price vs session open
        if current_close == self.session_open:
            return
        session_direction = 1 if current_close > self.session_open else -1

        # Bar direction from close vs open
        bar_direction = 1 if current_close > current_open else -1

        # Only trade trend-aligned moves (bar direction same as session direction)
        if bar_direction != session_direction:
            return

        # Bracket distances proportional to the range, with a floor
        stop_dist = max(0.25, self.stop_mult * current_range)
        target_dist = max(0.25, self.target_mult * current_range)

        # Enter in the direction of the move (which is also the session trend)
        if bar_direction > 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="trend-continuation-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="trend-continuation-short",
            )

        self.traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
