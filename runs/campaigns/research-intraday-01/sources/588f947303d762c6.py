"""
Refined intraday momentum strategy with improved signal detection.

Key improvements over #0:
1. Use close-to-close moves consistently (not mixing open-to-close)
2. More precise threshold calibration based on recent volatility percentile
3. Shorter holding period to capture quick continuation before mean reversion
4. Better session state management and position tracking
5. Tighter brackets to reduce slippage on exits
6. Only trade when the signal bar itself shows strong momentum direction

The hypothesis remains: after a bar with an unusually large close-to-close move
(relative to recent session volatility), the next bar tends to continue that
direction within a 2-3 bar window.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 10,
        move_percentile: float = 0.70,  # top 30% of recent moves
        hold_bars: int = 3,
        stop_offset: float = 0.75,
        target_offset: float = 1.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.move_percentile = move_percentile
        self.hold_bars = hold_bars
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size

        # State tracking
        self._session: int | None = None
        self._bars_held: int = 0
        self._closes: deque[float] = deque(maxlen=lookback + 2)
        self._position_open_ts: int | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset on session change
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_held = 0
            self._closes.clear()
            self._position_open_ts = None

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        # Update close tracking
        current_bar = history[-1]
        self._closes.append(current_bar.close)

        # Manage existing position
        if ctx.position:
            self._bars_held += 1
            if self._bars_held >= self.hold_bars:
                ctx.close(tag="max-hold")
                self._bars_held = 0
                self._position_open_ts = None
            return

        # Not in position; check for entry
        if ctx.working_order_ids:
            return

        # Need enough history to compute volatility
        if len(self._closes) < self.lookback + 1:
            return

        # Compute close-to-close moves for recent bars
        closes_list = list(self._closes)
        moves = [
            abs(closes_list[i] - closes_list[i - 1])
            for i in range(1, len(closes_list))
        ]

        if not moves or all(m == 0 for m in moves):
            return

        # Find the threshold: the move that is at the move_percentile level
        sorted_moves = sorted(moves)
        idx = int(len(sorted_moves) * self.move_percentile)
        idx = min(idx, len(sorted_moves) - 1)
        threshold_move = sorted_moves[idx]

        if threshold_move <= 0:
            return

        # Check the most recent close-to-close move (from bar -2 close to bar -1 close)
        # This is the move that just completed
        recent_cc_move = moves[-1]

        # The bar that just closed is history[-1]
        # The bar before that is history[-2]
        prev_bar = history[-2]
        curr_bar = history[-1]

        # Signal: the previous bar (now closed) had a large move
        # We check if it closed in the direction of that move (confirmation)
        prev_cc_move = moves[-2] if len(moves) >= 2 else 0

        # Enter if the bar that just closed had a large move AND closed in the
        # direction of continuation (not a reversal bar)
        if recent_cc_move >= threshold_move:
            # The move from bar[-2] close to bar[-1] close was large
            # Check if bar[-1] closed up or down from bar[-2]
            move_direction = curr_bar.close - prev_bar.close

            if move_direction > 0:
                # Upward move; expect continuation
                order_id = ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="momentum-long",
                )
                if order_id is not None:
                    self._bars_held = 0
                    self._position_open_ts = ctx.ts

            elif move_direction < 0:
                # Downward move; expect continuation
                order_id = ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="momentum-short",
                )
                if order_id is not None:
                    self._bars_held = 0
                    self._position_open_ts = ctx.ts

    def on_fill(self, ctx) -> None:
        # Reset hold counter on any fill (entry or exit)
        self._bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        # Handle order rejections and expirations
        if ctx.event.kind in ("order_rejected", "day_expired", "gtd_expired", "cancel_confirmed"):
            if ctx.event.order_id is not None:
                self._bars_held = 0
                self._position_open_ts = None
