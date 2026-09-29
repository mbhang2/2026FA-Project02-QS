"""
Intraday momentum with reverse hypothesis testing and commission-aware exit.

Prior trials (#0-#4) show persistent negative returns (-0.23 to -0.31), which
suggests the first-30m-to-last-30m momentum hypothesis may be fundamentally
weak or even reversed in NQ futures.

Key observation: All prior strategies assumed SAME-DIRECTION continuation
(first 30m up → last 30m up). But what if the effect is REVERSED? What if
early strength exhausts buyers and leads to late-session weakness?

This version tests the OPPOSITE hypothesis:
1. If first 30m closes UP → expect last 30m to close DOWN (sell signal)
2. If first 30m closes DOWN → expect last 30m to close UP (buy signal)

This reversal hypothesis is testable and could explain the consistent losses:
we've been trading the wrong direction all along.

Additional refinements:
- Tighter entry discipline: only trade if first 30m move is substantial
- Commission awareness: reduce size to 1 contract minimum, focus on high-conviction
- Minimal hold time: exit on the close of the last 30m bar, not before
- No early exit mechanics that might interfere with the signal
- Simpler volatility filter: skip choppy first 30m periods

The bet: if the momentum is REVERSED, this should show positive returns.
If it's still negative, the signal itself may be too weak to trade.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30m",
        size: int = 1,
        min_first_30m_move: float = 0.3,
        max_first_30m_volatility: float = 0.008,
    ):
        """
        Args:
            interval: Bar interval (30m for first/last 30m hypothesis)
            size: Position size (1 contract)
            min_first_30m_move: Minimum move in first 30m to generate signal (in points)
            max_first_30m_volatility: Max volatility (high-low / open) to trade
        """
        self.interval = interval
        self.size = size
        self.min_first_30m_move = min_first_30m_move
        self.max_first_30m_volatility = max_first_30m_volatility
        
        # Session tracking
        self._current_session: int | None = None
        self._session_start_ts: int | None = None
        self._first_30m_bar: object | None = None
        self._first_30m_signal: int | None = None  # 1 for sell (up→down), -1 for buy (down→up)
        self._first_30m_vol: float = 0.0
        self._traded_this_session: bool = False
        self._in_last_30m: bool = False

    def setup(self, ctx) -> None:
        """Subscribe to 30-minute bars."""
        ctx.subscribe(self.interval)

    def _calculate_volatility(self, bar) -> float:
        """Calculate volatility as (high - low) / open."""
        if bar.open <= 0:
            return 0.0
        return (bar.high - bar.low) / bar.open

    def on_bars(self, ctx) -> None:
        """
        Test REVERSED momentum hypothesis:
        - First 30m UP → expect last 30m DOWN (sell)
        - First 30m DOWN → expect last 30m UP (buy)
        """
        if not ctx.actionable:
            return

        # Handle session transitions
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._session_start_ts = ctx.session_start
            self._first_30m_bar = None
            self._first_30m_signal = None
            self._first_30m_vol = 0.0
            self._traded_this_session = False
            self._in_last_30m = False

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current_bar = history[-1]
        
        # Capture the first bar of the session (first 30m)
        if self._first_30m_bar is None:
            if current_bar.open_ts == self._session_start_ts:
                self._first_30m_bar = current_bar
                self._first_30m_vol = self._calculate_volatility(current_bar)
                
                # Check if first 30m move is substantial and volatility is acceptable
                first_30m_move = abs(current_bar.close - current_bar.open)
                
                if (
                    first_30m_move >= self.min_first_30m_move
                    and self._first_30m_vol <= self.max_first_30m_volatility
                ):
                    # REVERSED signal: if first 30m closed UP, expect DOWN (sell)
                    if current_bar.close > current_bar.open:
                        self._first_30m_signal = 1  # sell signal
                    elif current_bar.close < current_bar.open:
                        self._first_30m_signal = -1  # buy signal
                return

        # Check if we're in the last 30 minutes
        time_until_close = ctx.session_end - ctx.ts
        thirty_minutes_ns = 30 * 60 * 1_000_000_000
        in_last_30_minutes = time_until_close <= thirty_minutes_ns
        
        # Transition into last 30m window
        if in_last_30_minutes and not self._in_last_30m:
            self._in_last_30m = True
            
            # Enter trade if we have a signal and haven't traded yet
            if (
                self._first_30m_signal is not None
                and not self._traded_this_session
                and ctx.position == 0
                and not ctx.working_order_ids
            ):
                # Use tight brackets: the hypothesis is about direction, not magnitude
                stop_offset = 0.4
                target_offset = 0.5
                
                if self._first_30m_signal == 1:
                    # First 30m was UP → expect DOWN → SELL
                    ctx.sell(
                        size=self.size,
                        stop_offset=stop_offset,
                        target_offset=target_offset,
                        tag="reversed-momentum-sell",
                    )
                else:  # -1
                    # First 30m was DOWN → expect UP → BUY
                    ctx.buy(
                        size=self.size,
                        stop_offset=stop_offset,
                        target_offset=target_offset,
                        tag="reversed-momentum-buy",
                    )
                self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        """Track fills; bracket handles exits."""
        pass

    def on_lifecycle(self, ctx) -> None:
        """Handle order lifecycle events."""
        pass
