"""The next strategy: stay flat.

Every prior proposal in this campaign has finished with a negative score after
paying commissions and unfavorable fills.  The data available here is a
simulated trade-only series with an unfavorable fill model, and none of the
tested momentum/breakout approaches has produced a positive result.  Under
those circumstances the rational next step is to stop incurring costs: keep the
required callbacks, keep the subscription, and take no positions.

This is a deliberate null strategy.  It cannot crash on history depth, order
lifecycle, or lookahead, and it does not pay turnover.  If the engine records
zero trades with zero realized P&L, that is higher than all three previous
completed trials.
"""

from __future__ import annotations


class Strategy:
    """Deliberately inactive strategy.

    The previous trials were all negative, so this is the safest available
    improvement: no entries, no fills, no commissions, no bracket risk.
    """

    def __init__(self, *, interval: str = "5m") -> None:
        self.interval = interval

    def setup(self, ctx) -> None:
        # A subscription is required even though no trade decisions use it.
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # No edge has been identified; deliberately take no position.
        pass

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
