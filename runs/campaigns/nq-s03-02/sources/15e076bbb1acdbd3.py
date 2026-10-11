"""Investigate whether an outsized intra-session move gives back, not continues.

The campaign question asks whether a large move continues or gives back. The
prior trials all expressed the continuation claim and lost money. This strategy
expresses the opposite claim: after an outsized directional bar, the price
gives back some of that move. It enters in the opposite direction of the
outsized bar's close, with a tight bracket.

The give-back claim is more consistent with the prior results: continuation
strategies lost, and the losses were concentrated in a few sessions. A give-back
strategy should profit when those same sessions revert, and should also trade
more often because it can enter after any outsized bar, not just after a
continuation signal.

Changes from the prior trial:
- Entry is opposite the outsized bar's direction (give-back).
- The confirmation threshold is relaxed: the close must be in the outer 40% of
  the bar's range (close_position < 0.3 or > 0.7), not the outer 25%. This
  increases the number of trades while still requiring directional conviction.
- The trading window is widened slightly to include the first 30 minutes after
  the open, because give-back after an opening spike is a common pattern.
- The stop and target are tighter, reflecting the shorter expected holding
  period of a give-back trade.

All state is kept on self and only the single bar that just closed is read
from history, so no WindowTooShort crash can occur.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 6,
        range_mult: float = 1.6,
        min_typical_range: float = 0.4,
        size: int = 1,
        stop_offset: float = 1.5,
        target_offset: float = 2.0,
    ):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.min_typical_range = min_typical_range
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

        self._session = None
        self._ranges: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._ranges.clear()

        history = ctx.history(self.interval)
        if not history:
            return
        last = history[-1]

        bar_range = last.high - last.low
        if bar_range > 0:
            self._ranges.append(bar_range)
            if len(self._ranges) > self.lookback:
                self._ranges.pop(0)

        # Trade for most of the session, but skip the last 30 minutes to avoid
        # the close. Sessions run 17:00 CT to 16:00 CT, which is 22:00 UTC to
        # 21:00 UTC the next day. The last 30 minutes are 20:30-21:00 UTC.
        hour = ctx.now.hour
        minute = ctx.now.minute
        minutes_since_midnight = hour * 60 + minute
        if 20 * 60 + 30 <= minutes_since_midnight < 21 * 60:
            return

        if len(self._ranges) < self.lookback:
            return

        typical_range = sum(self._ranges) / len(self._ranges)
        if typical_range <= 0 or typical_range < self.min_typical_range:
            return

        if bar_range < self.range_mult * typical_range:
            return

        # Confirmation: close must be in the outer 40% of the bar's range
        bar_span = last.high - last.low
        if bar_span <= 0:
            return
        close_position = (last.close - last.low) / bar_span

        # Give-back: enter opposite the outsized bar's direction
        if close_position > 0.7:
            # Bar closed near its high: an outsized up move. Short it.
            if ctx.position > 0 or ctx.working_order_ids:
                return
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-giveback-short",
            )
        elif close_position < 0.3:
            # Bar closed near its low: an outsized down move. Buy it.
            if ctx.position < 0 or ctx.working_order_ids:
                return
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-giveback-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
