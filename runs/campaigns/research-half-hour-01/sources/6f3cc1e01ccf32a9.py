"""
Intraday momentum with mean-reversion hypothesis and adaptive entry timing.

All prior trials show persistent negative returns, suggesting the original
first-30m → last-30m momentum hypothesis is either too weak or wrong entirely.

Key insight from prior work:
- Trials #0-#2: Same-direction momentum = -0.23 to -0.31
- Trial #5: Reversed momentum = -0.35 (worse)
- Trial #4: Best so far at -0.13 (still negative, but 60% better)

Trial #4 worked better by:
1. Reducing trade frequency (volatility filter)
2. Exiting early (don't hold to session close)
3. Adjusting bracket sizes dynamically

This version tests a MEAN-REVERSION hypothesis instead:
- If first 30m is STRONG in one direction, expect mean reversion in last 30m
- But: only trade when the move is large (high confidence)
- And: enter DURING the last 30m window gradually, not all at once
- Exit: take profits quickly on any reversal signal, don't hold

The mechanism:
1. Measure first 30m strength: how far did it move from open?
2. Filter on volatility and move magnitude (high conviction only)
3. In last 30m: if price pulls back toward first 30m open, enter counter-trend
4. Use tight stops and quick exits to minimize commission drag
5. Scale position size inversely with volatility (trade less in choppy markets)

This is a pivot from momentum to mean-reversion, which is often stronger
in intraday timeframes. The key is high-conviction entry (strong first 30m
move) and quick exits (don't fight the trend, just scalp reversions).
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30m",
        base_size: int = 1,
        min_first_30m_move: float = 0.5,
        max_first_30m_vol: float = 0.012,
        reversion_threshold: float = 0.25,
        stop_offset: float = 0.4,
        target_offset: float = 0.6,
        max_hold_bars: int = 3,
    ):
        """
        Args:
            interval: Bar interval for first/last 30m hypothesis (30m)
            base_size: Base position size (1 contract minimum)
            min_first_30m_move: Minimum move in first 30m to generate signal (points)
            max_first_30m_vol: Max volatility to trade (high-low / open)
            reversion_threshold: How much price must pull back to enter (points)
            stop_offset: Stop loss distance from entry
            target_offset: Profit target distance from entry
            max_hold_bars: Maximum bars to hold position
        """
        self.interval = interval
        self.base_size = base_size
        self.min_first_30m_move = min_first_30m_move
        self.max_first_30m_vol = max_first_30m_vol
        self.reversion_threshold = reversion_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.max_hold_bars = max_hold_bars
        
        # Session state
        self._current_session: int | None = None
        self._session_start_ts: int | None = None
        self._first_30m_bar: object | None = None
        self._first_30m_open: float = 0.0
        self._first_30m_close: float = 0.0
        self._first_30m_high: float = 0.0
        self._first_30m_low: float = 0.0
        self._signal_direction: int | None = None  # 1=expect down, -1=expect up
        self._signal_strength: float = 0.0
        self._traded_this_session: bool = False
        self._entry_bar_count: int = 0
        self._session_high: float = 0.0
        self._session_low: float = 0.0

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
        Mean-reversion strategy: trade reversions from strong first 30m moves.
        """
        if not ctx.actionable:
            return

        # Handle session transitions
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._session_start_ts = ctx.session_start
            self._first_30m_bar = None
            self._first_30m_open = 0.0
            self._first_30m_close = 0.0
            self._first_30m_high = 0.0
            self._first_30m_low = 0.0
            self._signal_direction = None
            self._signal_strength = 0.0
            self._traded_this_session = False
            self._entry_bar_count = 0
            self._session_high = 0.0
            self._session_low = 0.0

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current_bar = history[-1]
        current_price = ctx.last_price
        
        # Track session high/low for context
        if self._session_high == 0.0:
            self._session_high = current_bar.high
            self._session_low = current_bar.low
        else:
            self._session_high = max(self._session_high, current_bar.high)
            self._session_low = min(self._session_low, current_bar.low)
        
        # Capture first 30m bar
        if self._first_30m_bar is None:
            if current_bar.open_ts == self._session_start_ts:
                self._first_30m_bar = current_bar
                self._first_30m_open = current_bar.open
                self._first_30m_close = current_bar.close
                self._first_30m_high = current_bar.high
                self._first_30m_low = current_bar.low
                
                # Evaluate signal: is the first 30m move strong enough?
                first_30m_move = abs(current_bar.close - current_bar.open)
                vol = self._calculate_volatility(current_bar)
                
                if (
                    first_30m_move >= self.min_first_30m_move
                    and vol <= self.max_first_30m_vol
                ):
                    # Strong, clean move: expect reversion
                    if current_bar.close > current_bar.open:
                        # First 30m UP → expect reversion DOWN
                        self._signal_direction = 1  # sell on reversion
                        self._signal_strength = first_30m_move
                    else:
                        # First 30m DOWN → expect reversion UP
                        self._signal_direction = -1  # buy on reversion
                        self._signal_strength = first_30m_move
                return

        # Check if we're in the last 30 minutes
        time_until_close = ctx.session_end - ctx.ts
        thirty_minutes_ns = 30 * 60 * 1_000_000_000
        in_last_30_minutes = time_until_close <= thirty_minutes_ns
        
        # Entry logic: during last 30m, look for reversion
        if (
            in_last_30_minutes
            and self._signal_direction is not None
            and not self._traded_this_session
            and ctx.position == 0
            and not ctx.working_order_ids
        ):
            # Check for reversion: price pulling back from the first 30m extreme
            if self._signal_direction == 1:
                # First 30m was UP → looking for pullback (price near first 30m open or below)
                pullback_distance = self._first_30m_high - current_price
                if pullback_distance >= self.reversion_threshold:
                    # Price has pulled back enough → sell (expect continued down)
                    ctx.sell(
                        size=self.base_size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="mean-reversion-sell",
                    )
                    self._traded_this_session = True
                    self._entry_bar_count = 0
            else:  # -1
                # First 30m was DOWN → looking for pullback (price near first 30m open or above)
                pullback_distance = current_price - self._first_30m_low
                if pullback_distance >= self.reversion_threshold:
                    # Price has pulled back enough → buy (expect continued up)
                    ctx.buy(
                        size=self.base_size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="mean-reversion-buy",
                    )
                    self._traded_this_session = True
                    self._entry_bar_count = 0

        # Position management: track hold time
        if ctx.position != 0:
            self._entry_bar_count += 1
            if self._entry_bar_count >= self.max_hold_bars:
                # Force exit after max hold time
                ctx.close(tag="max-hold-exit")
                self._entry_bar_count = 0

    def on_fill(self, ctx) -> None:
        """Track fills; bracket handles most exits."""
        fill = ctx.fill
        if fill.position == 0:
            self._entry_bar_count = 0

    def on_lifecycle(self, ctx) -> None:
        """Handle order lifecycle events."""
        pass
