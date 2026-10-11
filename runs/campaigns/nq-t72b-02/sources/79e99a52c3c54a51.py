"""Test whether an outsized counter-trend move within a session gives back,
refined with ATR-based volatility, order-flow divergence, and session-close
flattening.

Hypothesis: after a bar whose absolute close-open return is large relative to
recent average true range (ATR), and whose direction is opposite to the
session's prevailing trend, price tends to revert toward the session trend.
We measure "typical" with ATR rather than average absolute return, which is
more robust to outliers and adapts to changing volatility. We also require
order-flow divergence: the bar's signed delta must oppose its price direction,
indicating the move was driven by a temporary imbalance rather than genuine
flow. Bracket distances are multiples of ATR, with a larger target than stop
to let winners run. Finally, we flatten any open position near the session
close to avoid holding overnight.

We limit to one trade per session and require a minimum time remaining to
reduce overtrading and avoid entering too close to the close.
"""

from __future__ import annotations


class Strategy:
    """Mean reversion after outsized counter-trend intra-session moves."""

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
        self.session_bars: list[tuple[float, float, float, float, float, float]] = []
        # (open, high, low, close, volume, delta)
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
            (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.delta)
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

        # Compute ATR over the last min_bars bars (excluding current)
        prior_bars = self.session_bars[-(self.min_bars + 1):-1]
        atr_sum = 0.0
        prev_close = prior_bars[0][0]  # open of first prior bar as previous close
        for b in prior_bars:
            o, h, l, c, _, _ = b
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            atr_sum += tr
            prev_close = c
        atr = atr_sum / len(prior_bars)
        if atr <= 0:
            return

        # Current bar's move
        current_open, current_high, current_low, current_close, current_volume, current_delta = self.session_bars[-1]
        current_abs_ret = abs(current_close - current_open)

        # Is it outsized?
        if current_abs_ret <= self.threshold * atr:
            return
        if current_abs_ret < self.min_abs_move:
            return

        # Session direction: sign of current close vs average of last min_bars closes
        avg_close = sum(b[3] for b in prior_bars) / len(prior_bars)
        if current_close == avg_close:
            return
        session_direction = 1 if current_close > avg_close else -1

        # Bar direction
        bar_direction = 1 if current_close > current_open else -1

        # Only trade counter-trend moves (bar direction opposite to session direction)
        if bar_direction == session_direction:
            return

        # Order-flow divergence: delta sign must oppose bar direction
        if current_delta == 0:
            return
        if (current_delta > 0 and bar_direction > 0) or (current_delta < 0 and bar_direction < 0):
            return

        # Bracket distances proportional to ATR, with a floor
        stop_dist = max(0.25, self.stop_mult * atr)
        target_dist = max(0.25, self.target_mult * atr)

        # Enter in the direction of the session trend (opposite to the bar's move)
        if bar_direction > 0:
            # Up bar in a down session -> buy (expect reversion up)
            ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="counter-trend-giveback-long",
            )
        else:
            # Down bar in an up session -> sell (expect reversion down)
            ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="counter-trend-giveback-short",
            )

        self.traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
