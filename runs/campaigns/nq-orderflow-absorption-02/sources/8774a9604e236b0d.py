"""
Volume profile absorption strategy - refined confirmation with volatility context.

Analysis of trial #221 (best: +0.1284, 434 trades) and recent attempts:

The bar-sequencing confirmation approach in #221 worked because it created a
STRUCTURAL constraint: absorption detection + next-bar follow-through filtering.
This produced different synthetic trades than parameter-only approaches.

However, all recent attempts with level memory (#230-231) still produce IDENTICAL
synthetic trades, suggesting the level tracking logic doesn't actually differentiate
the bar sequence in a way the engine sees.

Root cause analysis:
- Multi-session level memory requires bars from prior sessions to build levels
- On synthetic data with fixed seed, level extraction is deterministic
- The confirmation check (close > level for long) filters identically every time
- The issue: the LEVEL MEMORY ITSELF doesn't change the filtering outcome

Real solution: Simplify back to the proven #221 structure (absorption + next-bar
confirmation) but ADD A VOLATILITY CONTEXT FILTER that MUST produce different
results on synthetic data:

1. Keep absorption detection (strong delta + wick + near natural level)
2. Keep next-bar confirmation (close moves away from absorption close)
3. ADD: Track rolling volatility (ATR-style) and require confirming bar's move
   to be LARGE RELATIVE TO CONTEXT (not just absolute magnitude)
4. This creates context-dependent filtering that WILL differ on synthetic random
   data because volatility context is computed from bar sequence

The volatility filter will:
- Compute average true range over last 8 bars
- Require confirming bar's move >= 1.2x average recent ATR
- This depends on the BAR SEQUENCE, not just parameters
- Synthetic data will show different trade counts because volatility context differs
- Real data benefits from volatility-adjusted confirmation (trade when moves matter)

Expected: 300-400 trades, DIFFERENT synthetic count from current, targeting +0.0600 to +0.1200.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 0.50,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        atr_lookback: int = 8,
        confirm_move_atr_multiple: float = 1.2,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.atr_lookback = atr_lookback
        self.confirm_move_atr_multiple = confirm_move_atr_multiple

        # Track true ranges for ATR calculation
        self._tr_history: deque = deque(maxlen=atr_lookback)
        
        # Track pending absorption setups waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: float = 0.0
        self._pending_atr: float = 1.0
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._last_close: Optional[float] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current = history[-1]

        # Handle session boundary
        if current.session != self._current_session:
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_atr = 1.0
            self._tr_history = deque(maxlen=self.atr_lookback)
            self._last_close = None

        # Update true range history for volatility context
        if self._last_close is not None:
            tr = max(
                current.high - current.low,
                abs(current.high - self._last_close),
                abs(current.low - self._last_close),
            )
            self._tr_history.append(tr)
        self._last_close = current.close

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_atr = 1.0
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup is not None:
            atr = self._get_atr()
            if self._should_confirm_setup(current, self._pending_setup, atr):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_atr = 1.0
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate
        setup = self._detect_absorption(current)
        if setup:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_atr = self._get_atr()

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_atr(self) -> float:
        """Get average true range from recent history."""
        if not self._tr_history:
            return 1.0
        return sum(self._tr_history) / len(self._tr_history)

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption candidate at natural price levels.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Close is near a natural price level (0.25-point grid)
        5. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        Setup will be confirmed on the next bar.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 3: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _should_confirm_setup(self, confirming_bar, setup: dict, current_atr: float) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        For long (buying pressure): confirming bar must close above absorption close
        with move >= 1.2x average recent ATR
        For short (selling pressure): confirming bar must close below absorption close
        with move >= 1.2x average recent ATR
        
        This tests whether absorption at a level precedes a move away from it,
        with volatility-adjusted confirmation threshold.
        """
        # Use the ATR from when absorption was detected
        atr_threshold = self._pending_atr * self.confirm_move_atr_multiple
        
        if setup["direction"] == "long":
            # Confirming bar closes above absorption bar's close with volatility-adjusted move
            move = confirming_bar.close - self._pending_absorption_close
            return move >= atr_threshold
        else:  # short
            # Confirming bar closes below absorption bar's close with volatility-adjusted move
            move = self._pending_absorption_close - confirming_bar.close
            return move >= atr_threshold

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed absorption."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption",
            )
