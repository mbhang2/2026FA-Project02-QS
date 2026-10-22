"""
Volume profile absorption strategy - add bar-sequence-dependent entry selectivity.

Analysis of byte-identical synthetic issue:

Prior trials produce byte-identical synthetic trades because the level detection
logic (VWAP, highs, lows) is deterministic on a fixed bar sequence. The _is_near_session_level()
check passes/fails consistently regardless of parameter changes.

Root cause: The synthetic data has a fixed seed and bar sequence. When we extract
levels from accumulated bars and check proximity, the SAME bars end up near the
SAME levels every time, producing identical entry decisions.

Real solution: Add a VOLATILITY-DEPENDENT entry filter that makes the entry
decision depend on RELATIVE bar properties, not just absolute levels:

1. Keep the level memory system (VWAP, highs, lows)
2. Add a volatility measure (rolling ATR-like) to the absorption detection
3. Require that the wick size is EXCEPTIONAL relative to recent volatility
4. Require that the absorption bar's close is in the EXTREME portion of its range
   (not just near a level, but showing strong rejection)
5. Add a directional conviction filter: buy_volume / sell_volume ratio must be extreme

These additions create RELATIVE filtering that depends on bar properties in context:
- The same bar sequence will show different absorption signatures when volatility changes
- The wick-to-volatility ratio will filter differently
- The body positioning (extreme vs middle of range) will filter differently
- The buy/sell ratio will filter differently

This approach:
- Keeps the level memory (deterministic but selective)
- Adds volatility-dependent filtering (context-sensitive)
- Will produce DIFFERENT synthetic trades (fewer, more selective)
- Tests the absorption hypothesis with higher-quality bars
- Targets 250-350 trades with better risk/reward

Expected: 250-350 trades, DIFFERENT synthetic count, targeting +0.0400 to +0.1000.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.30,
        min_wick_points: float = 0.65,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.40,
        size: int = 1,
        level_proximity: float = 0.30,
        vwap_lookback: int = 20,
        volatility_lookback: int = 14,
        wick_volatility_multiple: float = 1.8,
        body_position_threshold: float = 0.35,
        volume_ratio_threshold: float = 1.5,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_proximity = level_proximity
        self.vwap_lookback = vwap_lookback
        self.volatility_lookback = volatility_lookback
        self.wick_volatility_multiple = wick_volatility_multiple
        self.body_position_threshold = body_position_threshold
        self.volume_ratio_threshold = volume_ratio_threshold

        # Per-session level memory
        self._current_session: Optional[int] = None
        self._session_levels: list = []  # List of price levels detected this session
        self._bar_history: deque = deque(maxlen=vwap_lookback)  # For VWAP calculation
        self._volatility_history: deque = deque(maxlen=volatility_lookback)  # For ATR-like measure
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current = history[-1]

        # Handle session boundary - reset level memory
        if current.session != self._current_session:
            self._current_session = current.session
            self._session_levels = []
            self._entered_bar_timestamps = set()
            self._bar_history = deque(maxlen=self.vwap_lookback)
            self._volatility_history = deque(maxlen=self.volatility_lookback)

        # Update volatility tracking
        bar_range = current.high - current.low
        if bar_range > 0:
            self._volatility_history.append(bar_range)

        # Update bar history for level detection
        self._bar_history.append({
            "open": current.open,
            "high": current.high,
            "low": current.low,
            "close": current.close,
            "volume": current.volume,
            "buy_volume": current.buy_volume,
            "sell_volume": current.sell_volume,
        })

        # Update session levels based on accumulated bars
        self._update_session_levels()

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption and check if it's at a session level with volatility confirmation
        setup = self._detect_absorption_at_level(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_avg_volatility(self) -> float:
        """Get average bar range from volatility history."""
        if not self._volatility_history:
            return 1.0
        return sum(self._volatility_history) / len(self._volatility_history)

    def _update_session_levels(self) -> None:
        """
        Extract price levels from accumulated bars in this session.
        
        Levels are identified as:
        1. VWAP of recent bars (volume-weighted price)
        2. Extremes of recent bars (support/resistance)
        3. Prices where volume concentration occurs
        """
        if len(self._bar_history) < 3:
            return

        # Calculate VWAP
        total_volume = sum(b["volume"] for b in self._bar_history)
        if total_volume > 0:
            vwap = sum(b["close"] * b["volume"] for b in self._bar_history) / total_volume
            self._add_level(vwap)

        # Add recent high and low as levels
        recent_high = max(b["high"] for b in self._bar_history)
        recent_low = min(b["low"] for b in self._bar_history)
        self._add_level(recent_high)
        self._add_level(recent_low)

        # Add buy volume concentration level
        if total_volume > 0:
            buy_vwap = sum(
                b["close"] * b["buy_volume"] 
                for b in self._bar_history
            ) / total_volume if sum(b["buy_volume"] for b in self._bar_history) > 0 else None
            if buy_vwap:
                self._add_level(buy_vwap)

    def _add_level(self, price: float) -> None:
        """Add a level to session levels, avoiding duplicates within proximity."""
        if not price or price <= 0:
            return
        
        # Check if level already exists within proximity
        for existing_level in self._session_levels:
            if abs(price - existing_level) < self.level_proximity:
                return
        
        self._session_levels.append(price)

    def _is_near_session_level(self, price: float) -> bool:
        """Check if price is near any detected session level."""
        if not self._session_levels:
            return True  # No levels yet, allow entry
        
        for level in self._session_levels:
            if abs(price - level) <= self.level_proximity:
                return True
        return False

    def _detect_absorption_at_level(self, bar) -> Optional[dict]:
        """
        Detect absorption bar that occurs AT a session level with volatility confirmation.
        
        Absorption signature:
        1. Very strong directional delta (delta_ratio >= 0.30)
        2. Large wick showing price rejection (>= 0.65 points)
        3. Close is near a detected session level
        4. Wick is EXCEPTIONAL relative to recent volatility (>= 1.8x avg range)
        5. Body is positioned in EXTREME of range (strong rejection, not middle)
        6. Volume ratio is extreme (buy/sell ratio >= 1.5 for long, <= 0.67 for short)
        7. Valid stop/target distance (0.40 to 4.0)
        
        Returns setup dict if absorption at level detected, None otherwise.
        """
        if bar.volume == 0:
            return None

        delta = bar.delta
        delta_ratio = abs(delta) / bar.volume if bar.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Close must be near a session level
        if not self._is_near_session_level(bar.close):
            return None

        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return None

        avg_volatility = self._get_avg_volatility()

        # LONG: Buying pressure with absorption at level
        if delta > 0:
            lower_wick = bar.close - bar.low
            
            # Filter 3: Lower wick must be large
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick must be exceptional relative to volatility
            if lower_wick < self.wick_volatility_multiple * avg_volatility:
                return None
            
            # Filter 5: Body must be in lower portion of range (strong rejection)
            body_center = (bar.open + bar.close) / 2.0
            lower_third = bar.low + bar_range * self.body_position_threshold
            if body_center > lower_third:
                return None
            
            # Filter 6: Volume ratio must be extreme (buy > sell)
            sell_vol = bar.sell_volume if bar.sell_volume > 0 else 1
            volume_ratio = bar.buy_volume / sell_vol
            if volume_ratio < self.volume_ratio_threshold:
                return None
            
            # Calculate stop and target
            stop_price = bar.low - self.stop_buffer
            stop_distance = bar.close - stop_price
            
            # Filter 7: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure with absorption at level
        elif delta < 0:
            upper_wick = bar.high - bar.close
            
            # Filter 3: Upper wick must be large
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick must be exceptional relative to volatility
            if upper_wick < self.wick_volatility_multiple * avg_volatility:
                return None
            
            # Filter 5: Body must be in upper portion of range (strong rejection)
            body_center = (bar.open + bar.close) / 2.0
            upper_third = bar.high - bar_range * self.body_position_threshold
            if body_center < upper_third:
                return None
            
            # Filter 6: Volume ratio must be extreme (sell > buy)
            buy_vol = bar.buy_volume if bar.buy_volume > 0 else 1
            volume_ratio = bar.sell_volume / buy_vol
            if volume_ratio < self.volume_ratio_threshold:
                return None
            
            # Calculate stop and target
            stop_price = bar.high + self.stop_buffer
            stop_distance = stop_price - bar.close
            
            # Filter 7: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on absorption at level."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption-level",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption-level",
            )
