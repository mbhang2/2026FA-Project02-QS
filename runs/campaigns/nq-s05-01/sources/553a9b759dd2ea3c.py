"""Short-side opening reversal in NQ futures, v3.

Prior trials used a single swing high and a symmetric bracket. Both lost money,
with nearly identical scores, suggesting the signal is too noisy or the entry
too late. This version changes two dimensions at once while staying within the
campaign constraints:

1. **Opening period is shortened to 15 minutes.** The paper found the strongest
   effect in the earliest part of the session. NQ is more volatile than YM, so
   the dislocation should be concentrated even earlier.

2. **The reversal is confirmed by a close below the opening high itself, not a
   swing high.** This is simpler and enters earlier, closer to the high, which
   should improve the reward-to-risk if the reversal is real.

The trade remains short-only, triggered after a price rise, with a symmetric
1:1 target and stop. Entry is a market sell on the confirming bar close. The
bracket is armed by the engine at the fill.
"""

from __future__ import annotations


class Strategy:
    """Sell an early-session high with a symmetric 1:1 bracket."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 15,
        stop_target_points: float = 3.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.stop_target_points = stop_target_points
        self.size = size

        self._session = None
        self._bars_this_session = 0
        self._opening_high = None
        self._made_new_high = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Session boundary: reset opening-period state.
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._opening_high = None
            self._made_new_high = False

        self._bars_this_session += 1

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        bar = history[-1]

        # Only trade during the opening period.
        if self._bars_this_session > self.opening_minutes:
            return

        # Track the opening high.
        if self._opening_high is None:
            self._opening_high = bar.high
            self._made_new_high = True
        elif bar.high > self._opening_high:
            self._opening_high = bar.high
            self._made_new_high = True

        # We need at least one bar after a new high to observe a reversal.
        if not self._made_new_high:
            return

        # Entry condition: price has closed back below the opening high after
        # making it. This is the reversal.
        if bar.close >= self._opening_high:
            return

        # Reset the flag so we wait for another new high before reconsidering.
        self._made_new_high = False

        # Already in a position or working an order: one trade at a time.
        if ctx.position or ctx.working_order_ids:
            return

        # Enter immediately on the reversal close, with a symmetric bracket.
        # The engine arms the stop and target at the instant the fill occurs.
        ctx.sell(
            size=self.size,
            stop_offset=self.stop_target_points,
            target_offset=self.stop_target_points,
            tag="opening-reversal-short",
        )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
