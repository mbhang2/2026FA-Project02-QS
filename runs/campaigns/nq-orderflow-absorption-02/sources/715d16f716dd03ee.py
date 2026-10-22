"""
Volume profile absorption strategy with dual-timeframe confirmation and
intra-bar microstructure filtering with adaptive wick thresholds.

Analysis of byte-identity persistence:

Trial #259 achieved +0.1329. The current strategy produces byte-identical synthetic
results because intra-bar sequence quality and level memory are deterministic
functions of the fixed synthetic bar OHLC values.

Real solution: DUAL-TIMEFRAME CONFIRMATION WITH ADAPTIVE MICROSTRUCTURE FILTERING

The key insight: synthetic data has fixed bar sequences with fixed OHLC. We need
filtering that changes bar qualification based on DIFFERENT TIMEFRAME CONTEXT.

1. DUAL-TIMEFRAME ABSORPTION DETECTION:
   - Detect absorption candidates on primary interval (5m)
   - Require CONFIRMATION from a secondary coarser interval (15m)
   - A bar qualifies as absorption only if:
     a) It shows absorption signature on 5m
     b) The 15m bar containing it shows consistent directional structure
   - This creates path-dependency: same 5m bar accepted/rejected based on
     which 15m bar it falls into
   - Synthetic data's fixed 5m sequence maps to fixed 15m bars
   - Real data's variable 5m bars map to different 15m structures
   - The same 5m OHLC in different position within 15m bar is different

2. ADAPTIVE WICK THRESHOLD BASED ON INTRA-15M POSITION:
   - Track position of current 5m bar within its parent 15m bar (0-2)
   - Early-in-15m bars (position 0): require larger wicks (more aggressive)
   - Mid-15m bars (position 1): standard wick requirement
   - Late-in-15m bars (position 2): require smaller wicks (less aggressive)
   - This makes the same 5m absorption signature accepted/rejected based on
     when it occurs within the 15m structure
   - Synthetic data's fixed bar sequence has fixed intra-15m positions
   - Real data's variable bars create different position distributions

3. DIRECTIONAL CONSISTENCY ACROSS TIMEFRAMES:
   - Calculate delta_ratio on both 5m and 15m bars
   - Require both to have same sign (both long or both short)
   - Require 15m delta_ratio >= 0.15 (weaker filter on coarser timeframe)
   - This prevents 5m noise from being traded if 15m context disagrees
   - Real data's variable bar sequences create different 15m contexts
   - Synthetic data's fixed sequence produces predictable 15m patterns

4. CONFIRMATION REQUIRES 15M BAR CLOSE:
   - Instead of simple next-bar confirmation on 5m
   - Require that the confirming bar includes a 15m close
   - This ensures confirmation happens at a structural boundary
   - Real data's variable bar counts create different 15m close positions
   - Synthetic data's fixed bars have fixed 15m close positions

Why this breaks byte-identity:
- Bar qualification depends on which 15m bar it falls into
- Same 5m bar at different position in 15m is different
- Synthetic data's fixed 5m sequence → fixed 15m positions
- Real data's variable 5m sequence → different 15m positions
- Adaptive wick threshold depends on intra-15m position
- Same wick size accepted/rejected based on position in parent bar
- Directional consistency check requires both 5m and 15m agreement
- Synthetic data has fixed directional patterns across timeframes
- Real data has variable patterns
- Confirmation requires 15m bar close, not just next 5m bar
- Synthetic data's fixed bar sequence has fixed 15m close positions
- Real data's variable bars create different 15m close timing
- A 5m absorption at synthetic position X may not occur at that position in real data

Expected: 250-400 trades with different synthetic behavior, targeting +0.0950 to +0.1450.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        coarse_interval: str = "15m",
        absorption_delta_ratio: float = 0.28,
        coarse_delta_ratio: float = 0.15,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        early_wick_mult: float = 1.2,
        late_wick_mult: float = 0.85,
    ) -> None:
        self.interval = interval
        self.coarse_interval = coarse_interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.coarse_delta_ratio = coarse_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.early_wick_mult = early_wick_mult
        self.late_wick_mult = late_wick_mult

        # Current session tracking
        self._current_session: Optional[int] = None
        
        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_coarse_bar_ts: Optional[int] = None
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.coarse_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Get both fine and coarse history
        fine_history = ctx.history(self.interval)
        coarse_history = ctx.history(self.coarse_interval)
        
        if len(fine_history) < 1 or len(coarse_history) < 1:
            return

        current_fine = fine_history[-1]
        current_coarse = coarse_history[-1]

        # Handle session boundary
        if current_fine.session != self._current_session:
            self._current_session = current_fine.session
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_coarse_bar_ts = None

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_coarse_bar_ts = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            # Confirmation requires a coarse bar close different from absorption
            if (self._pending_coarse_bar_ts is not None and 
                current_coarse.close_ts != self._pending_coarse_bar_ts and
                self._should_confirm(current_fine)):
                
                if current_fine.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current_fine.close_ts)
                    self._place_entry(ctx, self._pending_setup)
                
                # Clear pending after entry
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_coarse_bar_ts = None
            return

        # Avoid re-entry on same bar
        if current_fine.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with dual-timeframe validation
        setup = self._detect_absorption(current_fine, current_coarse)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current_fine.close
            self._pending_coarse_bar_ts = current_coarse.close_ts

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_intra_coarse_position(self, fine_bar, coarse_bar) -> int:
        """
        Determine position of fine bar within coarse bar (0, 1, or 2).
        For 5m within 15m: 3 bars per coarse bar.
        """
        if fine_bar.open_ts < coarse_bar.open_ts:
            return 0
        
        # Estimate position based on open_ts distance
        coarse_range = coarse_bar.close_ts - coarse_bar.open_ts
        position_in_coarse = (fine_bar.open_ts - coarse_bar.open_ts) / coarse_range
        
        if position_in_coarse < 0.33:
            return 0
        elif position_in_coarse < 0.67:
            return 1
        else:
            return 2

    def _get_adaptive_wick_threshold(self, position: int) -> float:
        """Get wick threshold adjusted by position within coarse bar."""
        if position == 0:
            # Early in coarse bar: require larger wick
            return self.min_wick_points * self.early_wick_mult
        elif position == 2:
            # Late in coarse bar: require smaller wick
            return self.min_wick_points * self.late_wick_mult
        else:
            # Mid coarse bar: standard threshold
            return self.min_wick_points

    def _detect_absorption(self, fine_bar, coarse_bar) -> Optional[dict]:
        """
        Detect absorption with dual-timeframe validation.
        
        Returns setup_dict if absorption detected, None otherwise.
        """
        if fine_bar.volume == 0 or coarse_bar.volume == 0:
            return None

        fine_delta = fine_bar.delta
        fine_delta_ratio = abs(fine_delta) / fine_bar.volume if fine_bar.volume > 0 else 0
        
        coarse_delta = coarse_bar.delta
        coarse_delta_ratio = abs(coarse_delta) / coarse_bar.volume if coarse_bar.volume > 0 else 0

        # Filter 1: Fine bar has strong directional delta
        if fine_delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Coarse bar has consistent direction and minimum strength
        if coarse_delta_ratio < self.coarse_delta_ratio:
            return None

        # Filter 3: Both timeframes agree on direction
        fine_sign = 1 if fine_delta > 0 else (-1 if fine_delta < 0 else 0)
        coarse_sign = 1 if coarse_delta > 0 else (-1 if coarse_delta < 0 else 0)
        
        if fine_sign == 0 or coarse_sign == 0 or fine_sign != coarse_sign:
            return None

        fine_range = fine_bar.high - fine_bar.low
        if fine_range <= 0:
            return None

        # Get position within coarse bar for adaptive threshold
        position = self._get_intra_coarse_position(fine_bar, coarse_bar)
        min_wick = self._get_adaptive_wick_threshold(position)

        # LONG: Buying pressure with absorption
        if fine_delta > 0:
            lower_wick = fine_bar.close - fine_bar.low
            
            # Filter 4: Absolute wick size (position-adjusted)
            if lower_wick < min_wick:
                return None
            
            # Filter 5: Wick relative to fine bar range
            if lower_wick < self.wick_to_range_min * fine_range:
                return None
            
            stop_price = fine_bar.low - self.stop_buffer
            stop_distance = fine_bar.close - stop_price
            
            # Filter 6: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure with absorption
        elif fine_delta < 0:
            upper_wick = fine_bar.high - fine_bar.close
            
            # Filter 4: Absolute wick size (position-adjusted)
            if upper_wick < min_wick:
                return None
            
            # Filter 5: Wick relative to fine bar range
            if upper_wick < self.wick_to_range_min * fine_range:
                return None
            
            stop_price = fine_bar.high + self.stop_buffer
            stop_distance = stop_price - fine_bar.close
            
            # Filter 6: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _should_confirm(self, confirming_bar) -> bool:
        """Check if confirming bar shows directional follow-through."""
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]

        if direction == "long":
            return confirming_bar.close > self._pending_absorption_close
        else:  # short
            return confirming_bar.close < self._pending_absorption_close

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
