"""Continuation of outsized intra-session moves, with a time-based exit.

Hypothesis: after a move that is large relative to the session's own realised
range so far, price continues in the same direction. We measure "outsized" as
the absolute change over the last `lookback` bars exceeding a fraction
(`threshold_mult`) of the session's high-low range so far. When triggered, we
enter in the direction of that move with a protective bracket sized off the
move itself.

In addition to the bracket, we impose a maximum holding period (`max_hold_bars`)
after which we flatten the position if neither the stop nor the target has been
hit. This is intended to cut short trades that neither continue strongly nor
reverse enough to hit the stop, reducing the time capital is tied up in
sideways positions and potentially improving the average trade quality.

The session range is tracked incrementally from closed bars, so no lookahead.
We require a minimum number of bars and a minimum range before acting to avoid
noise. The bracket offsets are multiples of the triggering move, capped to
avoid absurd levels.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback: int = 3,
        threshold_mult: float = 0.5,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        min_bars: int = 5,
        min_range: float = 0.5,
        max_hold_bars: int = 12,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.threshold_mult = threshold_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_bars = min_bars
        self.min_range = min_range
        self.max_hold_bars = max_hold_bars
        self.size = size

        # Session state
        self.session = None
        self.session_high = None
        self.session_low = None
        self.bars_in_session = 0
        self.recent_closes: deque[float] = deque(maxlen=lookback + 1)

        # Entry tracking for time-based exit
        self.entry_id = None
        self.entry_bar = None

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return

        # Update session state
        if ctx.session != self.session:
            self.session = ctx.session
            self.session_high = None
            self.session_low = None
            self.bars_in_session = 0
            self.recent_closes.clear()
            self.entry_id = None
            self.entry_bar = None

        history = ctx.history(self.interval)
        if len(history) == 0:
            return
        bar = history[-1]  # the bar that just closed

        self.bars_in_session += 1
        if self.session_high is None or bar.high > self.session_high:
            self.session_high = bar.high
        if self.session_low is None or bar.low < self.session_low:
            self.session_low = bar.low
        self.recent_closes.append(bar.close)

        # Time-based exit: if we have an open position and have held too long
        if ctx.position and self.entry_bar is not None:
            if self.bars_in_session - self.entry_bar >= self.max_hold_bars:
                # Cancel any remaining bracket legs (they are reduce-only)
                if self.entry_id is not None:
                    legs = ctx.find_orders(parent_id=self.entry_id)
                    for leg in legs:
                        ctx.cancel(leg.id)
                ctx.close()
                self.entry_id = None
                self.entry_bar = None
                return

        # Need enough bars and a meaningful range
        if self.bars_in_session < self.min_bars:
            return
        session_range = self.session_high - self.session_low
        if session_range < self.min_range:
            return

        # Need enough closes to compute the move
        if len(self.recent_closes) < self.lookback + 1:
            return

        # Compute move over lookback bars
        move = self.recent_closes[-1] - self.recent_closes[0]
        if abs(move) < self.threshold_mult * session_range:
            return

        # Already in a position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Determine direction and place order with bracket
        stop_offset = min(self.stop_mult * abs(move), session_range * 0.5)
        target_offset = min(self.target_mult * abs(move), session_range * 1.0)
        if move > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="outsized-continuation-long",
            )
        else:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="outsized-continuation-short",
            )
        if order_id is not None:
            self.entry_id = order_id

    def on_fill(self, ctx):
        # When a position opens, record the bar at which it happened
        if ctx.position != 0 and self.entry_id is not None:
            # Only set entry_bar if this fill corresponds to our entry order
            # (we assume the fill is for the entry because we only have one
            # working order at a time)
            self.entry_bar = self.bars_in_session
        elif ctx.position == 0:
            # Position closed, reset entry tracking
            self.entry_id = None
            self.entry_bar = None

    def on_lifecycle(self, ctx):
        # If our entry order is cancelled or expires, clear the id
        if self.entry_id is not None and ctx.event.order_id == self.entry_id:
            if ctx.event.kind in ("cancel_confirmed", "day_expired", "gtd_expired", "order_rejected"):
                self.entry_id = None
                self.entry_bar = None
