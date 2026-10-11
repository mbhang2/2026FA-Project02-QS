"""Give-back with adaptive threshold and continuation confirmation.

After an outsized bar, enter opposite with a bracket. The threshold adapts to
the session's own bar ranges, and the session must have moved enough to make
the give-back meaningful. A continuation filter rejects bars that close beyond
the prior bar's range, which is where the move is still extending and a
counter-trade is premature.

Changes from #11:
- Require the outsized bar to be followed by a bar that closes back inside the
  prior range before entering, rather than entering immediately on the outsized
  bar. This gives the give-back a chance to begin before we commit.
- Widen the target slightly relative to the stop, since give-back moves are
  often smaller than the initial thrust.
- Keep the same adaptive percentile threshold and session-move filter.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        min_bars: int = 12,
        percentile: float = 0.75,
        stop_mult: float = 0.6,
        target_mult: float = 1.6,
        size: int = 1,
        min_time_left_ns: int = 45 * 60 * 1_000_000_000,
        session_open_skip_ns: int = 20 * 60 * 1_000_000_000,
        session_move_mult: float = 1.5,
    ):
        self.interval = interval
        self.min_bars = min_bars
        self.percentile = percentile
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.min_time_left_ns = min_time_left_ns
        self.session_open_skip_ns = session_open_skip_ns
        self.session_move_mult = session_move_mult

        self._session = None
        self._bars_this_session = 0
        self._ranges: list[float] = []
        self._session_open_price = None
        self._prev_high = None
        self._prev_low = None
        self._pending_outsized = False
        self._pending_direction = None  # "short" or "long"

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._ranges = []
            self._session_open_price = None
            self._prev_high = None
            self._prev_low = None
            self._pending_outsized = False
            self._pending_direction = None

        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        if self._session_open_price is None:
            self._session_open_price = bar.open

        self._bars_this_session += 1
        bar_range = bar.high - bar.low
        self._ranges.append(bar_range)

        prev_high = self._prev_high
        prev_low = self._prev_low
        self._prev_high = bar.high
        self._prev_low = bar.low

        if not ctx.actionable:
            return

        if ctx.ts - ctx.session_start < self.session_open_skip_ns:
            return

        if self._bars_this_session < self.min_bars:
            return

        if ctx.session_end - ctx.ts < self.min_time_left_ns:
            return

        if ctx.position or ctx.working_order_ids:
            return

        if bar.close == bar.open:
            return

        # If we have a pending outsized bar, look for a confirmation bar that
        # closes back inside the prior range.
        if self._pending_outsized and self._pending_direction is not None:
            if prev_high is None or prev_low is None:
                self._pending_outsized = False
                self._pending_direction = None
                return

            # Confirmation: the bar closes back inside the prior bar's range.
            if prev_low <= bar.close <= prev_high:
                stop_offset = max(bar_range * self.stop_mult, 0.25)
                target_offset = max(bar_range * self.target_mult, 0.25)

                if self._pending_direction == "short":
                    ctx.sell(
                        size=self.size,
                        stop_offset=stop_offset,
                        target_offset=target_offset,
                        tag="giveback-short",
                    )
                else:
                    ctx.buy(
                        size=self.size,
                        stop_offset=stop_offset,
                        target_offset=target_offset,
                        tag="giveback-long",
                    )

            # Clear the pending state regardless of confirmation.
            self._pending_outsized = False
            self._pending_direction = None
            return

        # Check for an outsized bar.
        sorted_ranges = sorted(self._ranges)
        idx = int(self.percentile * (len(sorted_ranges) - 1))
        threshold = sorted_ranges[idx]
        if threshold <= 0:
            return

        if bar_range < threshold:
            return

        session_move = abs(bar.close - self._session_open_price)
        if session_move < self.session_move_mult * threshold:
            return

        # Continuation filter: if the bar closes outside the prior bar's range,
        # the move is still extending and a counter-trade is premature. Instead
        # of entering, mark it as pending and wait for a confirmation bar.
        if prev_high is not None and prev_low is not None:
            if bar.close > prev_high or bar.close < prev_low:
                direction = "short" if bar.close > bar.open else "long"
                self._pending_outsized = True
                self._pending_direction = direction
                return

        # If it closed inside the prior range, it is not a clean continuation,
        # so skip it entirely for now.
        return

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
