"""Investigate whether an outsized intra-session move tends to continue or give back.

This strategy tests the give-back hypothesis: after a move that is large relative
to the session's own volatility, price tends to revert toward the session's
prior mean. The mechanism is claimed to operate throughout the session, not just
in specific hours, because the imbalance that creates an outsized move is a
temporary liquidity event regardless of time of day.

"Large" is measured as the absolute distance from the session's volume-weighted
average price (VWAP) to the current bar's close, expressed as a multiple of the
session's average true range so far. VWAP is used because it represents the
consensus price of the session; a move far from VWAP is more likely to represent
a temporary dislocation than a permanent repricing.

Entry is counter to the move: if price has risen far above VWAP, sell; if it has
fallen far below, buy. The bracket is sized from the same ATR, with the stop
closer than the target to give the reversion room to work while limiting risk.

The strategy does not condition on month, weekday, or hour. Any concentration in
the results is a property of the data, not of the strategy's design.
"""

from __future__ import annotations


class Strategy:
    """Trade give-back of outsized intra-session moves away from VWAP."""

    def __init__(self, *, interval: str = "5m", atr_mult: float = 2.0,
                 stop_atr: float = 1.0, target_atr: float = 2.0,
                 size: int = 1):
        self.interval = interval
        self.atr_mult = atr_mult
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.size = size

        # Per-session state.
        self._session = None
        self._atr_sum = 0.0
        self._bar_count = 0
        self._prev_close = None
        self._cum_pv = 0.0
        self._cum_vol = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset state when the session changes.
        if ctx.session != self._session:
            self._session = ctx.session
            self._atr_sum = 0.0
            self._bar_count = 0
            self._prev_close = None
            self._cum_pv = 0.0
            self._cum_vol = 0

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        # One position at a time; the bracket handles exits.
        if ctx.position or ctx.working_order_ids:
            return

        bar = history[-1]

        # Update VWAP components.
        typical_price = (bar.high + bar.low + bar.close) / 3.0
        self._cum_pv += typical_price * bar.volume
        self._cum_vol += bar.volume

        # Update ATR.
        if self._prev_close is None:
            self._prev_close = bar.open
        tr = max(bar.high - bar.low,
                 abs(bar.high - self._prev_close),
                 abs(bar.low - self._prev_close))
        self._atr_sum += tr
        self._bar_count += 1
        self._prev_close = bar.close

        if self._bar_count < 2 or self._cum_vol <= 0:
            return

        atr = self._atr_sum / self._bar_count
        if atr <= 0:
            return

        vwap = self._cum_pv / self._cum_vol
        distance = bar.close - vwap

        # Outsized move: distance from VWAP exceeds multiple of ATR.
        if abs(distance) < self.atr_mult * atr:
            return

        # Give-back: trade against the move.
        if distance > 0:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_atr * atr,
                target_offset=self.target_atr * atr,
                tag="giveback-short",
            )
        else:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_atr * atr,
                target_offset=self.target_atr * atr,
                tag="giveback-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
