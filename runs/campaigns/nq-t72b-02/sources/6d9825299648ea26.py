"""Test whether an outsized trend-aligned move with strong order flow continues,
using median baselines, volume confirmation, and a strong-close filter.

Hypothesis: after a bar whose absolute close-open return is large relative to
the median of recent typical bar moves, whose range (high-low) is also large
relative to the median range, whose signed delta is large relative to the
median absolute delta, whose total volume is above the median recent volume,
and whose close is near the extreme of the bar (indicating strong momentum),
price tends to continue in that direction. This refines the best prior
continuation strategy (#17) by adding a range and close-location filter to
avoid bars that had a large range but closed indecisively, and by giving the
trade more room with a wider target and tighter stop.

We measure "typical" as the median absolute close-open return, median range,
median absolute delta, and median volume of the last N bars in the same
session. When the current bar's absolute return exceeds a multiple of that
median, its range exceeds a multiple of the median range, its absolute delta
exceeds a multiple of the median absolute delta, its volume exceeds the
median volume, its close is near the high (for up moves) or low (for down
moves), and all move with the session direction, we enter in the direction
of the move, with a bracket for protection.

We limit to one trade per session and require a minimum time remaining to
avoid holding into the close. We also flatten any open position near the
session close to avoid overnight risk.
"""

from __future__ import annotations

import statistics


class Strategy:
    """Momentum after outsized trend-aligned moves with strong order flow,
    using median baselines, volume confirmation, and a strong-close filter."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        min_bars: int = 20,
        price_threshold: float = 2.5,
        range_threshold: float = 2.0,
        delta_threshold: float = 1.5,
        volume_threshold: float = 1.0,
        close_location: float = 0.6,  # fraction of bar range from open to close
        stop_mult: float = 1.0,
        target_mult: float = 2.5,
        size: int = 1,
        min_abs_move: float = 0.5,
        min_time_left_ns: int = 30 * 60 * 1_000_000_000,  # 30 minutes
        close_margin_ns: int = 5 * 60 * 1_000_000_000,    # 5 minutes before close
    ):
        self.interval = interval
        self.min_bars = min_bars
        self.price_threshold = price_threshold
        self.range_threshold = range_threshold
        self.delta_threshold = delta_threshold
        self.volume_threshold = volume_threshold
        self.close_location = close_location
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.min_abs_move = min_abs_move
        self.min_time_left_ns = min_time_left_ns
        self.close_margin_ns = close_margin_ns

        # Per-session state
        self.current_session: int | None = None
        self.session_open: float | None = None
        # (close, open, high, low, delta, volume)
        self.session_bars: list[tuple[float, float, float, float, float, int]] = []
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
        self.session_bars.append(
            (bar.close, bar.open, bar.high, bar.low, bar.delta, bar.volume)
        )

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

        # Baseline: median absolute return, median range, median absolute delta,
        # median volume of the last min_bars bars before current
        prior_bars = self.session_bars[-(self.min_bars + 1):-1]
        abs_rets = [abs(c - o) for c, o, _, _, _, _ in prior_bars]
        ranges = [h - l for _, _, h, l, _, _ in prior_bars]
        abs_deltas = [abs(d) for _, _, _, _, d, _ in prior_bars]
        volumes = [v for _, _, _, _, _, v in prior_bars]

        med_abs_ret = statistics.median(abs_rets)
        med_range = statistics.median(ranges)
        med_abs_delta = statistics.median(abs_deltas)
        med_volume = statistics.median(volumes)

        if med_abs_ret <= 0 or med_range <= 0 or med_abs_delta <= 0 or med_volume <= 0:
            return

        # Current bar's data
        current_close, current_open, current_high, current_low, current_delta, current_volume = self.session_bars[-1]
        current_abs_ret = abs(current_close - current_open)
        current_range = current_high - current_low
        current_abs_delta = abs(current_delta)

        # Is the price move outsized?
        if current_abs_ret <= self.price_threshold * med_abs_ret:
            return
        if current_abs_ret < self.min_abs_move:
            return

        # Is the range outsized?
        if current_range <= self.range_threshold * med_range:
            return

        # Is the delta outsized?
        if current_abs_delta <= self.delta_threshold * med_abs_delta:
            return

        # Is volume above the median?
        if current_volume <= self.volume_threshold * med_volume:
            return

        # Bar direction from close vs open
        if current_close == current_open:
            return
        bar_direction = 1 if current_close > current_open else -1

        # Strong close: close must be near the extreme in the direction of the move
        if bar_direction > 0:
            # Up bar: close should be near high
            if current_close < current_open + self.close_location * current_range:
                return
        else:
            # Down bar: close should be near low
            if current_close > current_open - self.close_location * current_range:
                return

        # Session direction based on current price vs session open
        if current_close == self.session_open:
            return
        session_direction = 1 if current_close > self.session_open else -1

        # Delta direction
        delta_direction = 1 if current_delta > 0 else -1

        # Only trade when price move, delta, and session trend are all aligned
        if bar_direction != session_direction or delta_direction != session_direction:
            return

        # Bracket distances proportional to the price move, with a floor
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
