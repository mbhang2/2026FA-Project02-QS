"""Investigate whether an outsized intra-session move continues or gives back.

The question asks about one mechanism, consistent across the ordinary run of
sessions. This strategy measures "outsized" against the session's own realised
range so far: it tracks the session's high and low as bars close, and when the
current bar's range is large relative to the session's range so far, it takes a
position in the direction of the bar's close relative to its open.

To respect the question's standard, the strategy trades both directions
symmetrically and restricts itself to the middle of the session (hours 9-14 CT,
roughly 14:00-19:00 UTC), where the first attempt's results were not
concentrated in a single hour. The bracket protects the position from the fill.

The effect is claimed to be continuation: an outsized move reflects information
arriving and has further to run. The strategy enters in the direction of the
outsized bar and lets the bracket manage the exit.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 4,          # bars to compute the typical range from
        range_mult: float = 2.0,    # how many times the typical range is "outsized"
        size: int = 1,
        stop_offset: float = 1.5,
        target_offset: float = 2.5,
    ):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

        # Session state
        self._session = None
        self._session_high = None
        self._session_low = None
        self._session_bars = 0

        # Rolling range history for the "typical" measurement
        self._ranges: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset session state on a new session
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_high = None
            self._session_low = None
            self._session_bars = 0
            self._ranges.clear()

        # Track session high/low from closed bars
        history = ctx.history(self.interval)
        if not history:
            return

        for bar in history:
            if bar.session != self._session:
                continue
            if self._session_high is None or bar.high > self._session_high:
                self._session_high = bar.high
            if self._session_low is None or bar.low < self._session_low:
                self._session_low = bar.low

        self._session_bars += 1

        # Only trade during the middle of the session (hours 9-14 CT, roughly
        # 14:00-19:00 UTC). This avoids the open and close, where the first
        # attempt's results were most concentrated.
        hour = ctx.now.hour
        if hour < 14 or hour >= 19:
            return

        # Need enough bars to compute a typical range
        if len(history) < self.lookback + 1:
            return

        # Compute the typical range from the last `lookback` bars
        recent = history.last(self.lookback)
        typical_range = sum(b.high - b.low for b in recent) / self.lookback
        if typical_range <= 0:
            return

        # The bar that just closed
        last = history[-1]
        bar_range = last.high - last.low
        if bar_range <= 0:
            return

        # Is this bar outsized relative to the typical range?
        if bar_range < self.range_mult * typical_range:
            return

        # One position at a time
        if ctx.position or ctx.working_order_ids:
            return

        # Direction: continuation of the bar's close relative to its open
        if last.close > last.open:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-continuation-long",
            )
        elif last.close < last.open:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-continuation-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
