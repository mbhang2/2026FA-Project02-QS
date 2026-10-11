"""Investigate whether an outsized move inside a session continues or gives back.

This variant changes the definition of "outsized" from a bar's range relative to
recent bars to a bar's range relative to the session's cumulative range so far.
The idea is that a single bar consuming an unusually large fraction of the
session's total movement is more likely to be a temporary imbalance that gives
back, so we fade it (enter opposite the bar's direction). This is a give-back
bet, directly opposed to the prior continuation bet.

Session state is reset on session boundaries, and we keep cumulative range and
the most recent bar on self so the bounded window is never a problem.
"""

from __future__ import annotations


class Strategy:
    """Fade an outsized bar, where outsized means a large fraction of session range."""

    def __init__(self, *, interval: str = "5m", fraction: float = 0.35,
                 stop_mult: float = 1.0, target_mult: float = 1.5,
                 size: int = 1):
        self.interval = interval
        self.fraction = fraction
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self._session = None
        self._cum_range = 0.0
        self._last_bar = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset session state on a new session.
        if ctx.session != self._session:
            self._session = ctx.session
            self._cum_range = 0.0
            self._last_bar = None

        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        bar = history[-1]
        rng = bar.high - bar.low
        if rng <= 0:
            return

        # Update cumulative session range with this bar's range.
        self._cum_range += rng

        # Need at least one prior bar to have a meaningful fraction.
        if self._last_bar is None:
            self._last_bar = bar
            return

        # Outsized means this bar's range is a large fraction of the session's
        # total range so far.
        if rng < self.fraction * self._cum_range:
            self._last_bar = bar
            return

        # Fade the move: enter opposite the bar's close-open direction.
        if bar.close == bar.open:
            self._last_bar = bar
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if bar.close > bar.open:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="outsized-fade-short")
        else:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="outsized-fade-long")

        self._last_bar = bar

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
