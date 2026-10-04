"""Short-side opening reversal in NQ futures, refined.

The previous attempt used a stop order at the swing high after a close below it.
That entry never fires in a normal reversal: price has already traded back down
through the level, so a sell stop above the market rests and waits for a rally
that may not come. The bracket is armed only on fill, so many potential trades
were skipped.

This version enters immediately with a market sell when the reversal condition
is met. The condition remains short-only and reversal-only: after the opening
high is established, price must close back below a short-term swing high. The
symmetric 1:1 bracket is given as offsets, so the engine arms it at the fill.

The opening period, swing lookback, and bracket distance are unchanged from the
prior trial to isolate the effect of the entry mechanism.
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
