"""Test whether an outsized trend-aligned move within a session continues,
using a z-score of absolute close-open returns and a volume confirmation.

Hypothesis: after a bar whose absolute close-open return is unusually large
relative to the recent distribution of such returns (z-score above a threshold),
and whose direction matches the session's prevailing trend, price tends to
continue. We measure "unusually large" as a z-score of the absolute return
against the mean and standard deviation of the last N bars in the same session.
We also require the bar's volume to be at least the average volume of the prior
N bars, to confirm that the move was supported by participation. The entry is
in the direction of the move (which is also the session trend), with a bracket
for protection.

This is a refinement of the best prior continuation strategy (#7), adding a
z-score normalization and a volume filter to improve signal quality while
maintaining high exposure. We limit to one trade per session and require a
minimum time remaining to avoid holding into the close. We also flatten any
open position near the session close to avoid overnight risk.
"""

from __future__ import annotations

import math


class Strategy:
    """Momentum after outsized trend-aligned moves with z-score and volume filter."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        min_bars: int = 20,
        z_threshold: float = 2.0,
        volume_mult: float = 1.0,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        size: int = 1,
        min_abs_move: float = 0.5,
        min_time_left_ns: int = 30 * 60 * 1_000_000_000,  # 30 minutes
        close_margin_ns: int = 5 * 60 * 1_000_000_000,    # 5 minutes before close
    ):
        self.interval = interval
        self.min_bars = min_bars
        self.z_threshold = z_threshold
        self.volume_mult = volume_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.min_abs_move = min_abs_move
        self.min_time_left_ns = min_time_left_ns
        self.close_margin_ns = close_margin_ns

        # Per-session state
        self.current_session: int | None = None
        self.session_open: float | None = None
        self.session_bars: list[tuple[float, float, int]] = []  # (close, open, volume)
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
        self.session_bars.append((bar.close, bar.open, bar.volume))

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

        # Prior bars (exclude current)
        prior_bars = self.session_bars[-(self.min_bars + 1):-1]
        prior_abs_returns = [abs(c - o) for c, o, _ in prior_bars]
        prior_volumes = [v for _, _, v in prior_bars]

        # Compute mean and std of absolute returns
        n = len(prior_abs_returns)
        mean_abs = sum(prior_abs_returns) / n
        variance = sum((r - mean_abs) ** 2 for r in prior_abs_returns) / n
        std_abs = math.sqrt(variance)

        # Current bar's data
        current_close, current_open, current_volume = self.session_bars[-1]
        current_abs_ret = abs(current_close - current_open)

        # Minimum absolute move filter
        if current_abs_ret < self.min_abs_move:
            return

        # Z-score of current absolute return
        if std_abs <= 0:
            return
        z_score = (current_abs_ret - mean_abs) / std_abs
        if z_score < self.z_threshold:
            return

        # Volume confirmation: current volume must be at least volume_mult * average prior volume
        avg_volume = sum(prior_volumes) / n
        if current_volume < self.volume_mult * avg_volume:
            return

        # Session direction based on current price vs session open
        if current_close == self.session_open:
            return
        session_direction = 1 if current_close > self.session_open else -1

        # Bar direction
        bar_direction = 1 if current_close > current_open else -1

        # Only trade trend-aligned moves (bar direction same as session direction)
        if bar_direction != session_direction:
            return

        # Bracket distances proportional to the move, with a floor
        stop_dist = max(0.25, self.stop_mult * current_abs_ret)
        target_dist = max(0.25, self.target_mult * current_abs_ret)

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
