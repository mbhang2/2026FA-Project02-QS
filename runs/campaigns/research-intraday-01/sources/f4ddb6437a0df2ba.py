"""
Intraday momentum continuation strategy testing single-bar move hypothesis.

The hypothesis: after a bar closes with a move larger than the session's recent
typical bar size (median absolute close-to-close change), the next 2-5 bars tend
to continue in that direction rather than reverse.

Strategy:
1. Track the median absolute close-to-close change over the last N bars per session
2. On each bar, check if the prior bar's move exceeded this median by a threshold
3. If so, enter in the direction of that move on the next bar
4. Hold for 2-5 bars or until a protective stop/target is hit
5. Use brackets to protect from the instant of fill

This tests the core claim: large-relative moves have continuation bias within
the intraday window, net of friction.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(self, *, interval: str = "5m", lookback: int = 8, 
                 move_threshold: float = 1.5, hold_bars: int = 4,
                 stop_offset: float = 1.0, target_offset: float = 1.5,
                 size: int = 1) -> None:
        self.interval = interval
        self.lookback = lookback  # bars to compute median volatility from
        self.move_threshold = move_threshold  # multiplier of median move
        self.hold_bars = hold_bars  # max bars to hold a position
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        
        # Track state across callbacks
        self._session: int | None = None
        self._bars_in_session: int = 0
        self._bars_held: int = 0
        self._entry_signal: str | None = None  # "long", "short", or None
        self._closes: deque[float] = deque(maxlen=lookback + 1)  # track closes for volatility

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        # Track session changes
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_in_session = 0
            self._bars_held = 0
            self._entry_signal = None
            self._closes.clear()
        
        self._bars_in_session += 1
        
        # Get history
        history = ctx.history(self.interval)
        if len(history) < 2:  # need at least 2 bars to compute a move
            return
        
        # Update close prices for volatility calculation
        current_bar = history[-1]
        self._closes.append(current_bar.close)
        
        # Manage existing position
        if ctx.position:
            self._bars_held += 1
            if self._bars_held >= self.hold_bars:
                # Exit after holding for max bars
                ctx.close(tag="max-hold")
                self._bars_held = 0
                self._entry_signal = None
            return
        
        # Not in a position; look for entry signal
        if ctx.working_order_ids:
            return  # pending entry order
        
        # Compute median absolute close-to-close move
        if len(self._closes) < self.lookback:
            return  # not enough history yet
        
        closes_list = list(self._closes)
        moves = [abs(closes_list[i] - closes_list[i-1]) 
                 for i in range(1, len(closes_list))]
        moves.sort()
        median_move = moves[len(moves) // 2]
        
        if median_move <= 0:
            return
        
        # Check if the previous bar's move was large relative to median
        prev_bar = history[-2]
        curr_bar = history[-1]
        prev_move = abs(prev_bar.close - prev_bar.open)
        
        # Also check the close-to-close move from bar before last to last bar
        if len(closes_list) >= 2:
            cc_move = abs(closes_list[-1] - closes_list[-2])
        else:
            cc_move = 0.0
        
        # Use the larger of open-to-close or close-to-close for the previous bar
        significant_move = max(prev_move, cc_move)
        
        if significant_move < median_move * self.move_threshold:
            return  # move not large enough
        
        # Determine direction: did the previous bar close up or down?
        if prev_bar.close > prev_bar.open:
            # Previous bar was up; expect continuation
            order_id = ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="momentum-long"
            )
            if order_id is not None:
                self._entry_signal = "long"
                self._bars_held = 0
        elif prev_bar.close < prev_bar.open:
            # Previous bar was down; expect continuation
            order_id = ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="momentum-short"
            )
            if order_id is not None:
                self._entry_signal = "short"
                self._bars_held = 0

    def on_fill(self, ctx) -> None:
        # Reset hold counter on fill
        self._bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        # Track order rejections/expirations
        if ctx.event.kind in ("order_rejected", "day_expired", "gtd_expired"):
            self._entry_signal = None
            self._bars_held = 0
