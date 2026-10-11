"""Investigate whether an outsized intra-session move tends to continue or give back.

Hypothesis: a move that is large relative to the session's own realized range so
far is more likely to continue than to revert, but only during the first half of
the session, when the session's character is still being established and the
move is more likely to reflect genuine information flow. The second half of the
session is excluded because late-session moves are more often position squaring
or liquidity-driven, which is a different mechanism.

The strategy measures "large" as the absolute return from the session open to
the current bar's close, expressed as a multiple of the session's average true
range so far. A move of at least 2.5 ATR is considered outsized. Entry is in the
direction of the move, with a bracket whose stop and target are sized from the
same ATR, so the risk is scaled to the session's own volatility.

This addresses the revision's first requirement: the mechanism is claimed to
operate only in the first half of the session, and the strategy says why. The
other two requirements (month and weekday consistency) are empirical questions
that the run record will answer; the strategy itself does not condition on
either, so any concentration that appears is a property of the data, not of the
strategy.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of outsized intra-session moves, first half only."""

    def __init__(self, *, interval: str = "5m", atr_mult: float = 2.5,
                 stop_atr: float = 1.5, target_atr: float = 2.0,
                 session_frac: float = 0.5, size: int = 1):
        self.interval = interval
        self.atr_mult = atr_mult
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.session_frac = session_frac
        self.size = size

        # State accumulated across bars within the current session.
        self._session = None
        self._session_open = None
        self._atr_sum = 0.0
        self._bar_count = 0
        self._prev_close = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset per-session state when the session changes.
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_open = None
            self._atr_sum = 0.0
            self._bar_count = 0
            self._prev_close = None

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        # Only consider the first half of the session.
        elapsed = ctx.ts - ctx.session_start
        total = ctx.session_end - ctx.session_start
        if total <= 0 or elapsed / total >= self.session_frac:
            return

        # One position at a time; the bracket handles exits.
        if ctx.position or ctx.working_order_ids:
            return

        bar = history[-1]
        if self._session_open is None:
            # First bar of the session we have seen; establish the open.
            # The session open is the first trade price, which is the open of
            # the first bar. Use the earliest available bar's open.
            self._session_open = history[0].open if len(history) > 0 else bar.open
            self._prev_close = bar.close
            self._bar_count = 1
            return

        # Compute true range for the bar that just closed.
        if self._prev_close is None:
            self._prev_close = bar.open
        tr = max(bar.high - bar.low,
                 abs(bar.high - self._prev_close),
                 abs(bar.low - self._prev_close))
        self._atr_sum += tr
        self._bar_count += 1
        self._prev_close = bar.close

        if self._bar_count < 2:
            return

        atr = self._atr_sum / self._bar_count
        if atr <= 0:
            return

        # The outsized move: distance from session open to current close.
        move = bar.close - self._session_open
        if abs(move) < self.atr_mult * atr:
            return

        if move > 0:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_atr * atr,
                target_offset=self.target_atr * atr,
                tag="continuation-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_atr * atr,
                target_offset=self.target_atr * atr,
                tag="continuation-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
