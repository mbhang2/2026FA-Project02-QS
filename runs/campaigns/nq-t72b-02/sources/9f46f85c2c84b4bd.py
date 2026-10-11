"""Test whether an outsized move inside a session continues.

Hypothesis: after a bar whose absolute close-open return is large relative to
recent average true range (ATR), price tends to continue in the same direction
as that bar. We measure "typical" with ATR over the last N bars in the same
session. When the current bar's absolute return exceeds a multiple of that ATR,
we enter in the direction of the bar (momentum), with a bracket for protection.
Bracket distances are multiples of ATR, with a larger target than stop to let
winners run. We flatten any open position near the session close to avoid
holding overnight.

We limit to one trade per session and require a minimum time remaining to
reduce overtrading and avoid entering too close to the close.
"""

from __future__ import annotations


class Strategy:
    """Momentum after outsized intra-session moves."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        min_bars: int = 20,
        threshold: float = 3.0,
        stop_mult: float = 1.0,
        target_mult: float = 2.0,
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

        # Compute ATR over the last min_bars bars (excluding current)
        prior_bars = self.session_bars[-(self.min_bars + 1):-1]
        atr_sum = 0.0
        prev_close = prior_bars[0][0]  # open of first prior bar as previous close
        for b in prior_bars:
            o, h, l, c = b
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            atr_sum += tr
            prev_close = c
        atr = atr_sum / len(prior_bars)
        if atr <= 0:
            return

        # Current bar's move
        current_open, current_high, current_low, current_close = self.session_bars[-1]
        current_abs_ret = abs(current_close - current_open)

        # Is it outsized?
        if current_abs_ret <= self.threshold * atr:
            return
        if current_abs_ret < self.min_abs_move:
            return

        # Bar direction
        bar_direction = 1 if current_close > current_open else -1

        # Bracket distances proportional to ATR, with a floor
        stop_dist = max(0.25, self.stop_mult * atr)
        target_dist = max(0.25, self.target_mult * atr)

        # Enter in the direction of the bar (momentum)
        if bar_direction > 0:
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

        self.traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
