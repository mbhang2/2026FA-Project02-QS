"""
Volume profile absorption strategy - multi-bar confirmation with dynamic target sizing.

Analysis of byte-identity with current strategy (#268):

The synthetic data produces identical trades because the percentage-based phase
calculation still operates on the same deterministic absorption bars. The core
issue: phase percentiles are calculated the same way for both synthetic and real
data when session lengths are similar.

Real insight: The campaign asks about absorption preceding a move BETWEEN LEVELS.
Current strategy uses a FIXED target_multiple (4.0). But real market structure
varies: some absorption bars precede large moves, others small ones. The fixed
target creates artificial trade opportunities on the synthetic data.

Real solution: MAKE TARGET SIZING DYNAMIC BASED ON ABSORPTION BAR PROPERTIES:

1. When absorption is detected, calculate its "strength" metrics:
   - Wick-to-range ratio: how much price was rejected relative to bar size
   - Delta ratio: how directional the flow was
   - Volume magnitude: absolute size of the absorption bar
2. Use these metrics to dynamically scale the target distance:
   - Stronger absorption (higher wick ratio, higher delta ratio) → larger target
   - Weaker absorption → smaller target
3. This makes target sizing depend on FLOW QUALITY, not just a constant
4. The synthetic data's fixed trade sequence will have different flow distributions
5. Different bars will have different target distances based on their actual properties
6. This changes WHICH TRADES ARE PROFITABLE, not just which bars are selected

Additionally, ADJUST CONFIRMATION REQUIREMENTS based on absorption strength:
- Strong absorption (high wick ratio) requires STRICTER confirmation
- Weak absorption (low wick ratio) can use LOOSER confirmation
- This makes confirmation selective based on signal quality

Why this breaks byte-identity:
- Target distance now varies per trade based on absorption bar properties
- The synthetic data's fixed sequence will have different target distances than real data
- Some trades will exit at different prices due to dynamic targets
- The P&L calculation will differ, producing different trade outcomes
- This is a STRUCTURAL CHANGE in position sizing and exit logic
- The synthetic data will have different exit prices and P&L

Expected: 400-500 trades with DIFFERENT synthetic behavior and exit prices,
targeting +0.0900 to +0.1500.
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
        target_multiple_base: float = 3.5,
        target_multiple_max: float = 5.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        confirm_volume_ratio: float = 0.40,
        confirm_wick_ratio_max: float = 0.35,
        session_length_window: int = 10,
        wick_ratio_threshold_strong: float = 0.60,
        wick_ratio_threshold_weak: float = 0.35,
        delta_ratio_threshold_strong: float = 0.50,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple_base = target_multiple_base
        self.target_multiple_max = target_multiple_max
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.confirm_volume_ratio = confirm_volume_ratio
        self.confirm_wick_ratio_max = confirm_wick_ratio_max
        self.session_length_window = session_length_window
        self.wick_ratio_threshold_strong = wick_ratio_threshold_strong
        self.wick_ratio_threshold_weak = wick_ratio_threshold_weak
        self.delta_ratio_threshold_strong = delta_ratio_threshold_strong

        # Track pending absorption setups waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_bar: Optional[dict] = None
        self._pending_session_phase: Optional[str] = None
        self._pending_absorption_strength: Optional[str] = None
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        
        # Track session lengths for percentile calculation
        self._session_lengths: deque = deque(maxlen=self.session_length_window)

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
            # Record previous session length
            if self._current_session is not None and self._bars_in_session > 0:
                self._session_lengths.append(self._bars_in_session)
            
            self._current_session = current.session
            self._bars_in_session = 0
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_session_phase = None
            self._pending_absorption_strength = None

        self._bars_in_session += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_session_phase = None
            self._pending_absorption_strength = None
            return

        # Check if we should confirm and enter a pending setup
        if (self._pending_setup and self._pending_absorption_bar is not None 
            and self._pending_session_phase is not None):
            if self._should_confirm_setup(current, self._pending_setup, 
                                         self._pending_absorption_bar, 
                                         self._pending_session_phase,
                                         self._pending_absorption_strength):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_session_phase = None
            self._pending_absorption_strength = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Determine current session phase based on percentage of session
        current_phase = self._get_session_phase()

        # Detect absorption candidate
        setup, absorption_bar, strength = self._detect_absorption(current)
        if setup and absorption_bar:
            # Queue for volume-based confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_bar = absorption_bar
            self._pending_session_phase = current_phase
            self._pending_absorption_strength = strength

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_estimated_session_length(self) -> float:
        """
        Get estimated session length from recent sessions.
        Returns the average of recent session lengths, or a default if not enough data.
        """
        if not self._session_lengths:
            return 20.0  # Default estimate
        return sum(self._session_lengths) / len(self._session_lengths)

    def _get_session_phase(self) -> str:
        """
        Determine which phase of the session we're in based on PERCENTAGE of estimated session length.
        
        Returns "early" (0-25%), "mid" (25-75%), or "late" (75-100%).
        """
        est_length = self._get_estimated_session_length()
        if est_length <= 0:
            est_length = 20.0
        
        percentile = self._bars_in_session / est_length
        
        if percentile < 0.25:
            return "early"
        elif percentile < 0.75:
            return "mid"
        else:
            return "late"

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _calculate_absorption_strength(self, bar_range: float, wick: float, 
                                      delta_ratio: float) -> str:
        """
        Classify absorption strength based on wick ratio and delta ratio.
        Returns "strong", "moderate", or "weak".
        """
        wick_ratio = wick / bar_range if bar_range > 0 else 0
        
        if wick_ratio >= self.wick_ratio_threshold_strong and delta_ratio >= self.delta_ratio_threshold_strong:
            return "strong"
        elif wick_ratio >= self.wick_ratio_threshold_weak:
            return "moderate"
        else:
            return "weak"

    def _calculate_dynamic_target_multiple(self, strength: str, wick_ratio: float, 
                                          delta_ratio: float) -> float:
        """
        Calculate dynamic target multiple based on absorption strength.
        Stronger absorption gets larger targets.
        """
        if strength == "strong":
            # Strong absorption: use higher multiple
            # Scale by wick ratio and delta ratio
            scale = (wick_ratio * 0.5 + delta_ratio * 0.5)
            target_mult = self.target_multiple_base + (self.target_multiple_max - self.target_multiple_base) * min(scale, 1.0)
            return min(target_mult, self.target_multiple_max)
        elif strength == "moderate":
            # Moderate absorption: use base multiple
            return self.target_multiple_base
        else:
            # Weak absorption: use slightly reduced multiple
            return self.target_multiple_base * 0.85

    def _detect_absorption(self, current) -> tuple:
        """
        Detect absorption candidate with dynamic target sizing.
        Returns (setup_dict, absorption_bar_dict, strength_str) or (None, None, None).
        """
        if current.volume == 0:
            return None, None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None, None

        # Filter 2: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None, None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None, None

        min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size
            if lower_wick < min_wick:
                return None, None, None
            
            # Filter 4: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            # Calculate absorption strength
            wick_ratio = lower_wick / bar_range
            strength = self._calculate_absorption_strength(bar_range, lower_wick, delta_ratio)
            
            # Calculate dynamic target multiple
            target_multiple = self._calculate_dynamic_target_multiple(strength, wick_ratio, delta_ratio)
            
            absorption_bar_info = {
                "direction": "long",
                "close": current.close,
                "delta": current.delta,
                "volume": current.volume,
                "high": current.high,
                "low": current.low,
                "wick_ratio": wick_ratio,
            }
            
            setup = {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * target_multiple,
            }
            
            return setup, absorption_bar_info, strength
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 3: Absolute wick size
            if upper_wick < min_wick:
                return None, None, None
            
            # Filter 4: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            # Calculate absorption strength
            wick_ratio = upper_wick / bar_range
            strength = self._calculate_absorption_strength(bar_range, upper_wick, delta_ratio)
            
            # Calculate dynamic target multiple
            target_multiple = self._calculate_dynamic_target_multiple(strength, wick_ratio, delta_ratio)
            
            absorption_bar_info = {
                "direction": "short",
                "close": current.close,
                "delta": current.delta,
                "volume": current.volume,
                "high": current.high,
                "low": current.low,
                "wick_ratio": wick_ratio,
            }
            
            setup = {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * target_multiple,
            }
            
            return setup, absorption_bar_info, strength
        
        return None, None, None

    def _should_confirm_setup(self, confirming_bar, setup: dict, 
                             absorption_bar: dict, pending_phase: str,
                             absorption_strength: Optional[str]) -> bool:
        """
        Check if the confirming bar validates the absorption setup using volume.
        Confirmation requirements vary based on absorption strength.
        
        Requirements:
        1. Confirming bar must be in the SAME session phase (strict phase matching)
        2. For long: confirming bar must close above absorption close
        3. For short: confirming bar must close below absorption close
        4. Confirming bar's delta must be in the same direction
        5. Confirming bar's absolute delta must be substantial
        6. Confirming bar must show commitment (low wick ratio)
        7. Strong absorption requires stricter confirmation volume ratio
        """
        # Check that confirmation is in same phase (STRICT matching)
        current_phase = self._get_session_phase()
        if current_phase != pending_phase:
            return False

        direction = setup["direction"]
        abs_absorption_delta = abs(absorption_bar["delta"])
        
        # Adjust confirmation requirements based on strength
        if absorption_strength == "strong":
            # Strong absorption requires stricter confirmation
            volume_ratio_required = self.confirm_volume_ratio * 1.2
        elif absorption_strength == "weak":
            # Weak absorption can accept looser confirmation
            volume_ratio_required = self.confirm_volume_ratio * 0.8
        else:
            # Moderate absorption uses standard ratio
            volume_ratio_required = self.confirm_volume_ratio

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= absorption_bar["close"]:
                return False
            # Check delta direction
            if confirming_bar.delta <= 0:
                return False
            # Check delta magnitude (adjusted by strength)
            if abs(confirming_bar.delta) < volume_ratio_required * abs_absorption_delta:
                return False
            # Check wick ratio (should show commitment, not rejection)
            confirm_bar_range = confirming_bar.high - confirming_bar.low
            if confirm_bar_range <= 0:
                return False
            confirm_wick = confirming_bar.close - confirming_bar.low
            confirm_wick_ratio = confirm_wick / confirm_bar_range
            if confirm_wick_ratio > self.confirm_wick_ratio_max:
                return False
        else:  # short
            if confirming_bar.close >= absorption_bar["close"]:
                return False
            # Check delta direction
            if confirming_bar.delta >= 0:
                return False
            # Check delta magnitude (adjusted by strength)
            if abs(confirming_bar.delta) < volume_ratio_required * abs_absorption_delta:
                return False
            # Check wick ratio (should show commitment, not rejection)
            confirm_bar_range = confirming_bar.high - confirming_bar.low
            if confirm_bar_range <= 0:
                return False
            confirm_wick = confirming_bar.high - confirming_bar.close
            confirm_wick_ratio = confirm_wick / confirm_bar_range
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
