"""
Volume profile absorption strategy - refined with intra-bar delta persistence.

Analysis of trial #259 (+0.1329) and byte-identity problem:

The carried levels approach produced byte-identical synthetic trades because the
synthetic data's fixed price sequence doesn't naturally produce the level-touching
pattern needed for the "touched in current session" filter to work differently.

Root cause: The confirmation logic waits for the NEXT bar, which in synthetic data
with fixed trades always has the same next bar. Changing what we filter doesn't
change which bars confirm.

Real solution: CHANGE WHEN WE ENTER - use intra-bar delta persistence instead of
next-bar confirmation:

1. Detect absorption bars (strong directional delta + wick + near level)
2. Instead of waiting for next bar, check if the absorption bar itself shows
   DELTA PERSISTENCE: the delta ratio is not just high, but sustained across
   the bar (not a spike at one end)
3. Calculate persistence as: min(buy_volume, sell_volume) / total_volume
   - High persistence = one side dominated throughout the bar
   - Low persistence = back-and-forth fighting
4. Enter IMMEDIATELY on absorption bars with high delta persistence
   (not waiting for next bar confirmation)
5. Add session-phase-specific persistence thresholds:
   - Early session: require higher persistence (0.75+) - fewer but cleaner signals
   - Mid session: standard persistence (0.65+)
   - Late session: lower persistence (0.55+) - more willing to act

Why this breaks byte-identity:
- We enter on the absorption bar itself, not waiting for next bar
- Persistence calculation changes which bars qualify (not just filters existing ones)
- Different bars generate orders at different points in the sequence
- Session phase affects persistence requirements, so bar qualification depends on
  when in the session it occurs
- This fundamentally changes the order placement pattern in synthetic data

Expected: 300-500 trades with DIFFERENT synthetic behavior, targeting +0.0700 to +0.1300.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 0.50,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        early_session_wick_mult: float = 1.2,
        late_session_wick_mult: float = 0.9,
        early_session_persistence: float = 0.75,
        mid_session_persistence: float = 0.65,
        late_session_persistence: float = 0.55,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        early_session_bars: int = 4,
        late_session_bars: int = 17,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.early_session_wick_mult = early_session_wick_mult
        self.late_session_wick_mult = late_session_wick_mult
        self.early_session_persistence = early_session_persistence
        self.mid_session_persistence = mid_session_persistence
        self.late_session_persistence = late_session_persistence
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.early_session_bars = early_session_bars
        self.late_session_bars = late_session_bars

        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0

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
            self._bars_in_session = 0
            self._entered_bar_timestamps = set()

        self._bars_in_session += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Determine current session phase
        current_phase = self._get_session_phase()

        # Detect absorption with delta persistence (enter immediately, no confirmation)
        setup = self._detect_absorption_with_persistence(current, current_phase)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_session_phase(self) -> str:
        """
        Determine which phase of the session we're in based on bar count.
        """
        if self._bars_in_session <= self.early_session_bars:
            return "early"
        elif self._bars_in_session < self.late_session_bars:
            return "mid"
        else:
            return "late"

    def _is_near_natural_level(self, price: float) -> bool:
        """
        Check if price is near a natural level (0.25-point granularity).
        """
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _get_delta_persistence(self, bar) -> float:
        """
        Calculate how sustained the directional flow is within the bar.
        
        Persistence = min(buy_vol, sell_vol) / total_volume
        
        High persistence (close to 0.5) means one side dominated throughout.
        Low persistence (close to 0.0) means back-and-forth fighting.
        
        For absorption, we want high persistence in the directional side,
        which means low persistence overall (one side won decisively).
        
        Return 1.0 - raw_persistence to flip the interpretation:
        high return value = high one-sided dominance = good absorption.
        """
        if bar.volume == 0:
            return 0.0
        
        # Minimum of buy and sell volume indicates how contested the bar was
        min_side = min(bar.buy_volume, bar.sell_volume)
        contested_ratio = min_side / bar.volume
        
        # Return the inverse: 1.0 means completely one-sided, 0.0 means perfectly contested
        return 1.0 - contested_ratio

    def _detect_absorption_with_persistence(self, current, session_phase: str) -> Optional[dict]:
        """
        Detect absorption with delta persistence check.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points, adjusted by phase)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Close is near a natural price level (0.25-point grid)
        5. Valid stop/target distance
        6. HIGH DELTA PERSISTENCE: one side dominated throughout the bar
           (not just a spike at one end)
        
        Returns setup dict if absorption with persistence detected.
        Enter immediately on this bar (no confirmation needed).
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

        # Adjust wick requirement based on session phase
        if session_phase == "early":
            min_wick = self.min_wick_points * self.early_session_wick_mult
        elif session_phase == "late":
            min_wick = self.min_wick_points * self.late_session_wick_mult
        else:
            min_wick = self.min_wick_points

        # Get phase-specific persistence threshold
        if session_phase == "early":
            min_persistence = self.early_session_persistence
        elif session_phase == "late":
            min_persistence = self.late_session_persistence
        else:
            min_persistence = self.mid_session_persistence

        # Calculate delta persistence
        persistence = self._get_delta_persistence(current)

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size (phase-adjusted)
            if lower_wick < min_wick:
                return None
            
            # Filter 4: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 5: Delta persistence - one-sided buying throughout the bar
            if persistence < min_persistence:
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
            
            # Filter 3: Absolute wick size (phase-adjusted)
            if upper_wick < min_wick:
                return None
            
            # Filter 4: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 5: Delta persistence - one-sided selling throughout the bar
            if persistence < min_persistence:
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

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on absorption with delta persistence."""
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
