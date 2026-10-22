"""
Volume profile absorption strategy - test session-level level tracking with multi-bar confirmation.

Analysis of prior trials:

The best performer (#221) scored +0.1284 with 434 trades using bar-sequencing confirmation.
However, many trials with confirmation logic produce byte-identical synthetic trades, suggesting
the confirmation bar checks (close > level) are filtering the same bars regardless of parameters.

Key insight from #221's success: The confirmation approach WORKS - it scored positive. But the
byte-identical issue suggests we need to:

1. Track CROSS-SESSION levels more carefully (not just current bar)
2. Add MULTI-BAR confirmation (not just next bar) to vary filtering
3. Use SESSION-LEVEL volume profiles to identify real absorption points
4. Require confirmation bars to show EFFICIENCY (movement per unit of flow)

Real solution: Implement proper session-level volume profile tracking:

1. Build volume profile from bars in current session
2. Extract natural levels (point of control, value area edges)
3. Detect when price arrives at these levels with absorption (strong delta + wick)
4. Require MULTI-BAR confirmation: next 2 bars must show consistent follow-through
5. Add efficiency check: confirming bars must move with less volume (price discovery, not accumulation)

This approach:
- Creates DIFFERENT synthetic behavior (session-level state changes across sessions)
- Tests the core hypothesis: absorption at volume levels precedes moves away
- Uses multi-bar confirmation (2 bars must confirm) instead of single-bar
- Incorporates efficiency (follow-through bars should have lower volume concentration)
- Targets the sweet spot: selective entry (high quality) with reasonable frequency

The key differences from #221:
- Session-level volume profile tracking (accumulate bars, extract levels)
- Multi-bar confirmation (next bar must start move, bar after must continue efficiently)
- Efficiency requirement on confirming bars (lower volume concentration = price discovery)
- More structured level detection (not just "near natural level" but actual volume nodes)

Expected: 300-500 trades on real data, DIFFERENT synthetic count, targeting +0.0500 to +0.1500.
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
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        profile_lookback: int = 20,
        efficiency_volume_ratio: float = 0.50,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.profile_lookback = profile_lookback
        self.efficiency_volume_ratio = efficiency_volume_ratio

        # Session-level volume profile tracking
        self._session_bars: deque = deque(maxlen=profile_lookback)
        self._extracted_levels: list = []
        
        # Track pending absorption setups waiting for multi-bar confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._confirmation_bars_seen: int = 0
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None

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
            self._session_bars = deque(maxlen=self.profile_lookback)
            self._extracted_levels = []
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._confirmation_bars_seen = 0

        # Accumulate bars for session volume profile
        self._session_bars.append({
            "high": current.high,
            "low": current.low,
            "close": current.close,
            "volume": current.volume,
            "delta": current.delta,
        })
        
        # Extract levels from accumulated bars (natural clustering)
        if len(self._session_bars) >= 3:
            self._extracted_levels = self._extract_volume_levels()

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._confirmation_bars_seen = 0
            return

        # Handle multi-bar confirmation
        if self._pending_setup and self._pending_absorption_close is not None:
            self._confirmation_bars_seen += 1
            
            # Check if we should confirm and enter (need 2 bars of confirmation)
            if self._confirmation_bars_seen == 1:
                # First confirmation bar: must start the move
                if self._should_start_move(current, self._pending_setup):
                    # Continue to second confirmation bar
                    return
                else:
                    # First bar didn't confirm, reset
                    self._pending_setup = None
                    self._pending_absorption_close = None
                    self._confirmation_bars_seen = 0
            elif self._confirmation_bars_seen == 2:
                # Second confirmation bar: must continue move efficiently
                if self._should_continue_move(current, self._pending_setup):
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                
                # Reset after attempting entry
                self._pending_setup = None
                self._pending_absorption_close = None
                self._confirmation_bars_seen = 0
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate at extracted levels
        setup = self._detect_absorption(current)
        if setup:
            # Queue for multi-bar confirmation
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._confirmation_bars_seen = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _extract_volume_levels(self) -> list:
        """
        Extract natural price levels from session's volume profile.
        
        Find price clusters where volume congregates (simple approach: 
        find local highs in close prices that had significant volume).
        """
        if len(self._session_bars) < 3:
            return []
        
        levels = []
        bars_list = list(self._session_bars)
        
        # Find bars with above-average volume
        avg_volume = sum(b["volume"] for b in bars_list) / len(bars_list)
        high_volume_bars = [b for b in bars_list if b["volume"] > avg_volume * 0.7]
        
        if high_volume_bars:
            # Extract close prices from high-volume bars as natural levels
            closes = [b["close"] for b in high_volume_bars]
            # Round to nearest 0.25 (natural grid)
            rounded = [round(c * 4) / 4 for c in closes]
            # Get unique levels
            levels = sorted(list(set(rounded)))
        
        return levels

    def _is_near_extracted_level(self, price: float) -> bool:
        """Check if price is near an extracted volume level."""
        if not self._extracted_levels:
            return False
        
        for level in self._extracted_levels:
            if abs(price - level) <= 0.50:
                return True
        return False

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption candidate at extracted volume levels.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Close is near an extracted volume level
        5. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        Setup will be confirmed on the next 2 bars.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Price near extracted volume level
        if not self._is_near_extracted_level(current.close):
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

    def _should_start_move(self, bar, setup: dict) -> bool:
        """
        Check if first confirmation bar starts the move.
        
        For long: bar must close above absorption close
        For short: bar must close below absorption close
        """
        if setup["direction"] == "long":
            return bar.close > self._pending_absorption_close
        else:
            return bar.close < self._pending_absorption_close

    def _should_continue_move(self, bar, setup: dict) -> bool:
        """
        Check if second confirmation bar continues move efficiently.
        
        Efficiency: move should happen with relatively low volume concentration
        (indicating price discovery, not accumulation/distribution).
        """
        if bar.volume == 0:
            return False
        
        direction = setup["direction"]
        
        if direction == "long":
            # Must continue higher
            if bar.close <= self._pending_absorption_close:
                return False
            
            # Volume concentration should be moderate (not heavy accumulation)
            # Check that buy_volume is not overwhelming the move
            buy_ratio = bar.buy_volume / bar.volume if bar.volume > 0 else 0
            if buy_ratio > 0.85:  # Too much buying = accumulation, not discovery
                return False
            
            return True
        
        else:  # short
            # Must continue lower
            if bar.close >= self._pending_absorption_close:
                return False
            
            # Volume concentration should be moderate
            sell_ratio = bar.sell_volume / bar.volume if bar.volume > 0 else 0
            if sell_ratio > 0.85:  # Too much selling = distribution, not discovery
                return False
            
            return True

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
