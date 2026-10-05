"""Opening-range breakout with prior-session bias, entering on a pullback
that retraces a fixed fraction of the breakout bar's range, with a
time-based cancellation. This variant refines the entry timing: instead of
placing the limit immediately after the breakout bar, it waits for an
additional confirmation bar that closes beyond the breakout level in the
direction of the bias, then places the limit at a more conservative
pullback level (0.382 * breakout bar range).

The campaign question: when price leaves the opening range, does the move hold
or does the range win? This strategy answers "the move holds" in the direction
of the prior session's bias, on the ordinary run of sessions.

Changes from #19 (best so far, +0.2149):
- Add a confirmation bar requirement: after a breakout bar closes beyond the
  opening range, the next bar must also close beyond that level in the same
  direction. This filters out one-bar false breakouts.
- The limit entry is placed at a deeper pullback level (0.382 instead of 0.5)
  to improve fill probability and reduce adverse selection.
- Keep the time-based cancellation at 3 bars after confirmation.
- Keep the bracket offsets based on the opening range.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        daily_interval: str = "1d",
        opening_minutes: int = 30,
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 1.5,
        pullback_mult: float = 0.382,
        patience_bars: int = 3,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_minutes = opening_minutes
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.pullback_mult = pullback_mult
        self.patience_bars = patience_bars

        # State per session
        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._opening_range = None
        self._session_bars = 0
        self._breakout_direction = None  # 'long' or 'short' after breakout
        self._breakout_bar_range = None
        self._breakout_bar_close = None
        self._awaiting_confirmation = False
        self._limit_price = None
        self._entry_tag = None
        self._bars_since_confirmation = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.daily_interval)

    def on_bars(self, ctx) -> None:
        # Daily bar closes at session end -- not a decision point for entries.
        if self.interval not in ctx.bars:
            return

        if not ctx.actionable:
            return

        # Session tracking
        if ctx.session != self._session:
            self._session = ctx.session
            self._opening_high = None
            self._opening_low = None
            self._opening_range = None
            self._session_bars = 0
            self._breakout_direction = None
            self._breakout_bar_range = None
            self._breakout_bar_close = None
            self._awaiting_confirmation = False
            self._limit_price = None
            self._entry_tag = None
            self._bars_since_confirmation = 0

        self._session_bars += 1
        bar = ctx.bars[self.interval]

        # Build the opening range from the first `opening_minutes` of bars.
        opening_bars = self.opening_minutes // 5
        if self._session_bars <= opening_bars:
            if self._opening_high is None or bar.high > self._opening_high:
                self._opening_high = bar.high
            if self._opening_low is None or bar.low < self._opening_low:
                self._opening_low = bar.low
            if self._session_bars == opening_bars:
                if self._opening_high is not None and self._opening_low is not None:
                    self._opening_range = self._opening_high - self._opening_low
            return

        if self._opening_range is None or self._opening_range <= 0:
            return

        # If we already have a position, the bracket handles exits.
        if ctx.position:
            return

        # If we have a working limit order, check if it should be cancelled.
        if ctx.working_order_ids:
            self._bars_since_confirmation += 1
            if self._bars_since_confirmation >= self.patience_bars:
                ctx.cancel_all()
                self._breakout_direction = None
                self._breakout_bar_range = None
                self._breakout_bar_close = None
                self._awaiting_confirmation = False
                self._limit_price = None
                self._entry_tag = None
                self._bars_since_confirmation = 0
            return

        # Prior session daily bar for direction.
        daily = ctx.history(self.daily_interval)
        if len(daily) < 2:
            return
        prev = daily[-2]
        bias = prev.close - prev.open
        if bias == 0:
            return

        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return

        # If we are awaiting confirmation, check if this bar confirms.
        if self._awaiting_confirmation:
            if self._breakout_direction == "long":
                if bar.close > self._opening_high:
                    # Confirmed: place limit at pullback from breakout bar.
                    self._limit_price = self._breakout_bar_close - self.pullback_mult * self._breakout_bar_range
                    self._entry_tag = "pullback-long"
                    self._bars_since_confirmation = 0
                    self._awaiting_confirmation = False
                else:
                    # Failed confirmation, reset.
                    self._breakout_direction = None
                    self._breakout_bar_range = None
                    self._breakout_bar_close = None
                    self._awaiting_confirmation = False
            else:  # short
                if bar.close < self._opening_low:
                    self._limit_price = self._breakout_bar_close + self.pullback_mult * self._breakout_bar_range
                    self._entry_tag = "pullback-short"
                    self._bars_since_confirmation = 0
                    self._awaiting_confirmation = False
                else:
                    self._breakout_direction = None
                    self._breakout_bar_range = None
                    self._breakout_bar_close = None
                    self._awaiting_confirmation = False

        # If we have no breakout direction, look for a new breakout.
        if self._breakout_direction is None and not self._awaiting_confirmation:
            if bar.close > self._opening_high and bias > 0:
                self._breakout_direction = "long"
                self._breakout_bar_range = bar_range
                self._breakout_bar_close = bar.close
                self._awaiting_confirmation = True
            elif bar.close < self._opening_low and bias < 0:
                self._breakout_direction = "short"
                self._breakout_bar_range = bar_range
                self._breakout_bar_close = bar.close
                self._awaiting_confirmation = True

        # Place the limit order if we have a confirmed breakout.
        if self._breakout_direction is not None and not self._awaiting_confirmation:
            if self._breakout_direction == "long":
                if ctx.last_price > self._limit_price:
                    ctx.buy(
                        size=self.size,
                        type="limit",
                        limit=self._limit_price,
                        stop_offset=self.stop_mult * self._opening_range,
                        target_offset=self.target_mult * self._opening_range,
                        tag=self._entry_tag,
                    )
            else:  # short
                if ctx.last_price < self._limit_price:
                    ctx.sell(
                        size=self.size,
                        type="limit",
                        limit=self._limit_price,
                        stop_offset=self.stop_mult * self._opening_range,
                        target_offset=self.target_mult * self._opening_range,
                        tag=self._entry_tag,
                    )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
