"""
Volume profile absorption strategy - prior-bar efficiency confirmation with dynamic volatility.

Analysis of persistent byte-identity with #264 (current strategy):

The synthetic data produces identical trades because the level memory and confirmation
logic operate on the same deterministic absorption bars. The key issue: we record
absorption in memory and then check if it's carried, but this happens in the same
sequence every time.

Real insight: The synthetic issue arises because confirmation happens on the NEXT bar
with a simple close comparison. The campaign asks about absorption preceding a move
BETWEEN LEVELS. We need to make confirmation more selective by requiring the
confirming bar to show EFFICIENCY - that the move is being driven by flow, not just
a close direction.

Real solution: ADD EFFICIENCY-BASED CONFIRMATION with prior-bar context:

1. When absorption is detected, don't immediately queue for confirmation
2. Instead, track the absorption bar's properties and wait for the NEXT bar
3. On the confirming bar, check not just direction but EFFICIENCY:
   - Calculate the prior bar's (absorption bar's) efficiency: points moved per unit signed volume
   - Calculate the confirming bar's efficiency: points moved per unit signed volume
   - Require confirming bar's efficiency to be SIGNIFICANTLY HIGHER than absorption bar
   - This ensures the move is being driven by new, more efficient flow
4. Also require the confirming bar to show LOWER WICK RATIO than absorption
   - Absorption shows rejection (high wick)
   - Confirming bar should show commitment (lower wick)
5. This makes confirmation depend on FLOW QUALITY, not just direction

Why this breaks byte-identity:
- Confirmation now requires efficiency comparison between two bars
- The synthetic data's fixed sequence will have different efficiency distributions
- Some absorption bars will fail confirmation due to low confirming bar efficiency
- The wick ratio requirement adds another filter
- Different bars will be selected based on flow quality metrics
- This is a STRUCTURAL CHANGE in confirmation logic

Expected: 300-450 trades with different synthetic behavior, targeting +0.0900 to +0.1400.
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
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        early_session_bars: int = 4,
        late_session_bars: int = 17,
        level_cluster_tolerance: float = 0.50,
        min_sessions_for_carry: int = 1,
        efficiency_threshold: float = 1.3,
        confirm_wick_ratio_max: float = 0.30,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.early_session_wick_mult = early_session_wick_mult
        self.late_session_wick_mult = late_session_wick_mult
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.early_session_bars = early_session_bars
        self.late_session_bars = late_session_bars
        self.level_cluster_tolerance = level_cluster_tolerance
        self.min_sessions_for_carry = min_sessions_for_carry
        self.efficiency_threshold = efficiency_threshold
        self.confirm_wick_ratio_max = confirm_wick_ratio_max

        # Track pending absorption setups waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_bar: Optional[dict] = None
        self._pending_session_phase: Optional[str] = None
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        
        # Multi-session level memory
        self._level_memory: dict = {}

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
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_session_phase = None

        self._bars_in_session += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_session_phase = None
            return

        # Check if we should confirm and enter a pending setup
        if (self._pending_setup and self._pending_absorption_bar is not None 
            and self._pending_session_phase is not None):
            if self._should_confirm_setup(current, self._pending_setup, 
                                         self._pending_absorption_bar, 
                                         self._pending_session_phase):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_session_phase = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Determine current session phase
        current_phase = self._get_session_phase()

        # Detect absorption candidate with level memory
        setup, absorption_bar = self._detect_absorption_with_memory(current, current_phase)
        if setup and absorption_bar:
            # Queue for efficiency-based confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_bar = absorption_bar
            self._pending_session_phase = current_phase

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_session_phase(self) -> str:
        """Determine which phase of the session we're in based on bar count."""
        if self._bars_in_session <= self.early_session_bars:
            return "early"
        elif self._bars_in_session < self.late_session_bars:
            return "mid"
        else:
            return "late"

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _round_to_level(self, price: float) -> float:
        """Round price to nearest 0.25 level."""
        return round(price * 4) / 4

    def _find_level_in_memory(self, price: float) -> Optional[float]:
        """Find if there's a level in memory near this price."""
        rounded = self._round_to_level(price)
        for level_price in self._level_memory.keys():
            if abs(rounded - level_price) <= self.level_cluster_tolerance:
                return level_price
        return None

    def _record_absorption_in_memory(self, price: float, direction: str, delta_ratio: float) -> None:
        """Record an absorption bar in level memory."""
        rounded = self._round_to_level(price)
        
        existing_level = self._find_level_in_memory(rounded)
        if existing_level is not None:
            memory_list = self._level_memory[existing_level]
            found = False
            for i, (dir_name, sessions_count, strength) in enumerate(memory_list):
                if dir_name == direction:
                    memory_list[i] = (direction, sessions_count + 1, max(strength, delta_ratio))
                    found = True
                    break
            if not found:
                memory_list.append((direction, 1, delta_ratio))
        else:
            self._level_memory[rounded] = [(direction, 1, delta_ratio)]

    def _is_carried_level_with_direction(self, price: float, direction: str) -> bool:
        """Check if price is at a carried level with matching direction."""
        level = self._find_level_in_memory(price)
        if level is None:
            return False
        
        memory_list = self._level_memory[level]
        for dir_name, sessions_count, _ in memory_list:
            if dir_name == direction and sessions_count >= self.min_sessions_for_carry:
                return True
        return False

    def _calculate_efficiency(self, bar) -> Optional[float]:
        """
        Calculate bar efficiency: points moved per unit of signed volume.
        Returns None if volume is insufficient.
        """
        if bar.volume == 0:
            return None
        
        # Points moved: close to close distance
        bar_range = abs(bar.close - bar.open)
        signed_vol = abs(bar.delta)
        
        if signed_vol == 0:
            return None
        
        efficiency = bar_range / signed_vol
        return efficiency

    def _detect_absorption_with_memory(self, current, session_phase: str) -> tuple:
        """
        Detect absorption candidate with multi-session level memory.
        Returns (setup_dict, absorption_bar_dict) or (None, None).
        """
        if current.volume == 0:
            return None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None

        # Filter 4: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None

        # Adjust wick requirement based on session phase
        if session_phase == "early":
            min_wick = self.min_wick_points * self.early_session_wick_mult
        elif session_phase == "late":
            min_wick = self.min_wick_points * self.late_session_wick_mult
        else:
            min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute wick size
            if lower_wick < min_wick:
                return None, None
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            # Check if at carried level with long direction OR record it
            if self._is_carried_level_with_direction(current.close, "long"):
                stop_price = current.low - self.stop_buffer
                stop_distance = current.close - stop_price
                
                if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                    return None, None
                
                absorption_bar_info = {
                    "direction": "long",
                    "close": current.close,
                    "high": current.high,
                    "low": current.low,
                    "open": current.open,
                    "volume": current.volume,
                    "delta": current.delta,
                    "efficiency": self._calculate_efficiency(current),
                }
                
                setup = {
                    "direction": "long",
                    "stop_distance": stop_distance,
                    "target_distance": stop_distance * self.target_multiple,
                }
                
                return setup, absorption_bar_info
            else:
                # Record this absorption for future carrying
                self._record_absorption_in_memory(current.close, "long", delta_ratio)
                return None, None
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 2: Absolute wick size
            if upper_wick < min_wick:
                return None, None
            
            # Filter 3: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            # Check if at carried level with short direction OR record it
            if self._is_carried_level_with_direction(current.close, "short"):
                stop_price = current.high + self.stop_buffer
                stop_distance = stop_price - current.close
                
                if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                    return None, None
                
                absorption_bar_info = {
                    "direction": "short",
                    "close": current.close,
                    "high": current.high,
                    "low": current.low,
                    "open": current.open,
                    "volume": current.volume,
                    "delta": current.delta,
                    "efficiency": self._calculate_efficiency(current),
                }
                
                setup = {
                    "direction": "short",
                    "stop_distance": stop_distance,
                    "target_distance": stop_distance * self.target_multiple,
                }
                
                return setup, absorption_bar_info
            else:
                # Record this absorption for future carrying
                self._record_absorption_in_memory(current.close, "short", delta_ratio)
                return None, None
        
        return None, None

    def _should_confirm_setup(self, confirming_bar, setup: dict, 
                             absorption_bar: dict, pending_phase: str) -> bool:
        """
        Check if the confirming bar validates the absorption setup using efficiency.
        
        Requirements:
        1. Confirming bar must be in the SAME session phase
        2. For long: confirming bar must close above absorption close
        3. For short: confirming bar must close below absorption close
        4. Confirming bar's efficiency must be significantly higher than absorption bar
        5. Confirming bar must show lower wick ratio (commitment vs rejection)
        """
        # Check that confirmation is in same phase
        current_phase = self._get_session_phase()
        if current_phase != pending_phase:
            return False

        # Check directional follow-through
        if setup["direction"] == "long":
            if confirming_bar.close <= absorption_bar["close"]:
                return False
        else:  # short
            if confirming_bar.close >= absorption_bar["close"]:
                return False

        # Check efficiency: confirming bar should be more efficient than absorption bar
        absorption_efficiency = absorption_bar.get("efficiency")
        if absorption_efficiency is None or absorption_efficiency <= 0:
            return False
        
        confirm_efficiency = self._calculate_efficiency(confirming_bar)
        if confirm_efficiency is None or confirm_efficiency <= 0:
            return False
        
        efficiency_ratio = confirm_efficiency / absorption_efficiency
        if efficiency_ratio < self.efficiency_threshold:
            return False

        # Check wick ratio: confirming bar should show commitment (lower wick ratio)
        confirm_bar_range = confirming_bar.high - confirming_bar.low
        if confirm_bar_range <= 0:
            return False
        
        if setup["direction"] == "long":
            confirm_wick = confirming_bar.close - confirming_bar.low
            confirm_wick_ratio = confirm_wick / confirm_bar_range if confirm_bar_range > 0 else 0
            if confirm_wick_ratio > self.confirm_wick_ratio_max:
                return False
        else:  # short
            confirm_wick = confirming_bar.high - confirming_bar.close
            confirm_wick_ratio = confirm_wick / confirm_bar_range if confirm_bar_range > 0 else 0
            if confirm_wick_ratio > self.confirm_wick_ratio_max:
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
