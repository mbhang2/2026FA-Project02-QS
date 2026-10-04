"""Short-side opening reversal pattern in NQ futures.

Adapted from the paper doi-10.2139_ssrn.7124578, which documents a reversal
after an initial up-move in Dow futures, concentrated in the opening minutes.

The strategy is short-only, sells after a price rise, and uses a symmetric 1:1
risk/reward bracket. It defines the opening period as the first 30 minutes of
the session, tracks the high established in that period, and enters a short when
the market trades above that high by a small trigger amount after the opening
period has elapsed. The stop and target are symmetric offsets from the entry
price.

The first 30 minutes are measured with 5-minute bars, so the opening high is
the high of the first six bars. Entry is triggered on the close of a bar that
exceeds the opening high, with a fixed offset to avoid entering on noise.
"""

from __future__ import annotations


class Strategy:
    """Short-only opening-range reversal with a symmetric bracket."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_bars: int = 6,          # 6 * 5m = 30 minutes
        trigger_offset: float = 2.0,    # points above the opening high to enter
        stop_offset: float = 4.0,       # symmetric stop
        target_offset: float = 4.0,     # symmetric target
        size: int = 1,
    ):
        self.interval = interval
        self.opening_bars = opening_bars
        self.trigger_offset = trigger_offset
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size

        self._session = None
        self._bars_this_session = 0
        self._opening_high = None
        self._entered_this_session = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Track session and bar count
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._opening_high = None
            self._entered_this_session = False

        self._bars_this_session += 1

        history = ctx.history(self.interval)
        if not history:
            return
        bar = history[-1]

        # During the opening window, just record the high
        if self._bars_this_session <= self.opening_bars:
            if self._opening_high is None or bar.high > self._opening_high:
                self._opening_high = bar.high
            return

        # After the opening window, wait for a close above the opening high
        if self._opening_high is None:
            return

        if not ctx.actionable:
            return

        if ctx.position or ctx.working_order_ids:
            return

        if self._entered_this_session:
            return

        # Trigger: close above opening high plus offset
        if bar.close > self._opening_high + self.trigger_offset:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="opening-reversal-short",
            )
            self._entered_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
