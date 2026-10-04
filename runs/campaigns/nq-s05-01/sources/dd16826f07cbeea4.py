"""Short-side opening reversal in NQ futures.

Adapts the idea from doi-10.2139_ssrn.7124578: if the market establishes a high
early in the session, sell that high with a symmetric 1:1 target and stop.

The strategy is short-only and reversal-only. It defines the opening period as
the first 30 minutes of the session using 1-minute bars. A high is defined as
the highest high of the opening period so far, and the entry is triggered when
price closes back below a short-term swing high after having made a new opening
high. The stop and target are symmetric distances measured in index points.

The trade is placed as a stop order at the swing high, so it fills only if price
returns to that level, confirming the reversal. The bracket is armed by the
engine at the instant the entry fills.
"""

from __future__ import annotations


class Strategy:
    """Sell an early-session high with a symmetric 1:1 bracket."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 30,
        swing_lookback: int = 3,
        stop_target_points: float = 4.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.swing_lookback = swing_lookback
        self.stop_target_points = stop_target_points
        self.size = size

        self._session = None
        self._bars_this_session = 0
        self._opening_high = None
        self._swing_high = None
        self._swing_high_bars_ago = 0

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
            self._swing_high = None
            self._swing_high_bars_ago = 0

        self._bars_this_session += 1

        history = ctx.history(self.interval)
        if len(history) < self.swing_lookback + 1:
            return

        bar = history[-1]

        # Only trade during the opening period.
        if self._bars_this_session > self.opening_minutes:
            return

        # Track the opening high.
        if self._opening_high is None or bar.high > self._opening_high:
            self._opening_high = bar.high

        # Track a short-term swing high: a bar whose high exceeds the prior
        # swing_lookback bars' highs.
        window = history.last(self.swing_lookback + 1)
        current_high = window[-1].high
        prior_highs = [b.high for b in window[:-1]]
        if current_high > max(prior_highs):
            self._swing_high = current_high
            self._swing_high_bars_ago = 0
        else:
            self._swing_high_bars_ago += 1

        if self._swing_high is None:
            return

        # The swing high must be at least as high as the opening high so far,
        # and it must still be recent enough to act on.
        if self._swing_high < self._opening_high:
            return
        if self._swing_high_bars_ago > self.swing_lookback:
            return

        # Entry condition: price has closed back below the swing high after
        # making it, i.e., a reversal from the early high.
        if bar.close >= self._swing_high:
            return

        # Already in a position or working an order: one trade at a time.
        if ctx.position or ctx.working_order_ids:
            return

        # Sell on a stop at the swing high, so we enter if price returns to it.
        # The bracket gives symmetric 1:1 risk/reward.
        ctx.sell(
            size=self.size,
            type="stop",
            trigger=self._swing_high,
            stop_offset=self.stop_target_points,
            target_offset=self.stop_target_points,
            tag="opening-reversal-short",
        )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
