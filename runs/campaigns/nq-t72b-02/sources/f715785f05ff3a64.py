"""Test whether an outsized trend-aligned move that also breaks the recent
range continues.

Hypothesis: after a bar whose absolute close-open return is large relative to
recent average true range (ATR), whose direction matches the session's
prevailing trend, and whose close breaks the prior N-bar high/low, price tends
to continue in that direction. The breakout condition filters for moves that
are not just large but also establish a new extreme, which may indicate
stronger momentum. We use ATR for volatility normalization, which is more
robust to outliers than average absolute return. Bracket distances are
multiples of ATR, with a 1:1.5 risk-reward ratio. We limit to one trade per
session and flatten near the close to avoid overnight risk.

Parameters are chosen to maintain high exposure (one trade per session in
most sessions) while improving signal quality over the previous continuation
strategy (#7). The breakout filter is the main addition; threshold and
bracket multiples are kept similar to #7 to avoid overfitting.
"""

from __future__ import annotations


class Strategy:
    """Momentum after outsized trend-aligned range breakouts."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        min_bars: int = 20,
        threshold: float = 2.5,
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
        self.session_bars: list[tuple[float, float, float, float, float]] = []
        # (open, high, low, close, volume)
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
        self.session_bars.append((bar.open, bar.high, bar.low, bar.close, bar.volume))

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

        # Need enough prior bars to establish ATR and breakout range
        if len(self.session_bars) < self.min_bars + 2:
            return

        # Check time remaining in session
        if ctx.session_end - ctx.ts < self.min_time_left_ns:
            return

        # Compute ATR over the last min_bars bars (excluding current)
        prior_bars = self.session_bars[-(self.min_bars + 1):-1]
        prev_close = self.session_bars[-(self.min_bars + 2)][3]  # close before prior window
        atr_sum = 0.0
        for b in prior_bars:
            o, h, l, c, _ = b
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            atr_sum += tr
            prev_close = c
        atr = atr_sum / len(prior_bars)
        if atr <= 0:
            return

        # Current bar's data
        current_open, current_high, current_low, current_close, current_volume = self.session_bars[-1]
        current_abs_ret = abs(current_close - current_open)

        # Is it outsized?
        if current_abs_ret <= self.threshold * atr:
            return
        if current_abs_ret < self.min_abs_move:
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

        # Breakout condition: close must exceed prior N-bar high/low
        prior_highs = [b[1] for b in prior_bars]
        prior_lows = [b[2] for b in prior_bars]
        if bar_direction > 0:
            if current_close <= max(prior_highs):
                return
        else:
            if current_close >= min(prior_lows):
                return

        # Bracket distances proportional to ATR, with a floor
        stop_dist = max(0.25, self.stop_mult * atr)
        target_dist = max(0.25, self.target_mult * atr)

        # Enter in the direction of the move (which is also the session trend)
        if bar_direction > 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="trend-breakout-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="trend-breakout-short",
            )

        self.traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
