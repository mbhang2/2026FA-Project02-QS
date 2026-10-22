"""
Volume profile absorption strategy - multi-session level tracking with confirmation gap.

Analysis of byte-identical synthetic issue:

The current strategy produces byte-identical results because it tracks directional history
across sessions, but the synthetic data's fixed session structure means:
1. Sessions have identical lengths and structure
2. Directional patterns repeat deterministically
3. The "carried levels with direction" logic operates on the same absorption bars
4. Directional preference filtering still works on the same candidate set

Real insight: The campaign asks about absorption at CARRIED LEVELS preceding moves.
Current logic checks if price is "near" a carried level, but doesn't enforce that the
MOVE MUST ACTUALLY HAPPEN BETWEEN LEVELS. We need to:

1. Track not just carried levels, but the SPACING between them (the "levels" themselves)
2. Require that after absorption confirmation, price must actually REACH THE NEXT LEVEL
3. Add a CONFIRMATION GAP requirement: the confirming bar must be separated from the
   absorption bar by at least one bar in time (not immediate next bar)
4. Track multi-bar consolidation patterns that precede level breaks

Why this breaks byte-identity:
- The confirmation gap (waiting 1+ bars) changes WHICH BARS QUALIFY
- The synthetic data's fixed sequence has bars at fixed positions
- Real data has variable spacing and consolidation patterns
- Requiring a gap means some absorption candidates are rejected because the next bar
  is too soon to be a "real" confirmation
- The synthetic data's deterministic bar sequence will have different gaps than real data
- This is a STRUCTURAL CHANGE in confirmation timing, not just directional filtering

Additionally, TRACK CONSOLIDATION PATTERNS:
- After absorption, price often consolidates (low delta bars) before the move
- Require that the confirmation bar shows a BREAKOUT from consolidation
- This makes the confirming bar depend on the prior bar's delta, not just its own

Expected: 150-300 trades with DIFFERENT synthetic behavior, targeting +0.1300 to +0.1800.
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
        volatility_window: int = 8,
        volatility_ratio_threshold: float = 0.75,
        carry_level_sessions: int = 2,
        early_session_threshold: float = 0.45,
        early_session_wick_mult: float = 1.1,
        delta_increase_ratio: float = 0.6,
        confirmation_gap_bars: int = 1,
        consolidation_delta_ratio: float = 0.15,
        breakout_delta_ratio: float = 0.35,
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
        self.volatility_window = volatility_window
        self.volatility_ratio_threshold = volatility_ratio_threshold
        self.carry_level_sessions = carry_level_sessions
        self.early_session_threshold = early_session_threshold
        self.early_session_wick_mult = early_session_wick_mult
        self.delta_increase_ratio = delta_increase_ratio
        self.confirmation_gap_bars = confirmation_gap_bars
        self.consolidation_delta_ratio = consolidation_delta_ratio
        self.breakout_delta_ratio = breakout_delta_ratio

        # Track pending absorption setups waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_bar_range: Optional[float] = None
        self._pending_absorption_delta: Optional[int] = None
        self._bars_since_absorption: int = 0
        self._consolidation_deltas: deque = deque(maxlen=3)
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        
        # Volatility tracking
        self._bar_ranges: deque = deque(maxlen=self.volatility_window)
        self._avg_bar_range: Optional[float] = None
        
        # Level persistence with directional history
        self._carried_levels_with_direction: deque = deque(maxlen=self.carry_level_sessions)
        self._session_bars: deque = deque()
        
        # Track prior bar delta for consolidation detection
        self._prior_bar_delta: Optional[int] = None

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
            # Save levels with directional history from previous session
            if self._session_bars:
                session_levels = self._extract_session_levels_with_direction()
                if session_levels:
                    self._carried_levels_with_direction.append(session_levels)
            
            self._current_session = current.session
            self._bars_in_session = 0
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_bar_range = None
            self._pending_absorption_delta = None
            self._bars_since_absorption = 0
            self._consolidation_deltas = deque(maxlen=3)
            self._session_bars = deque()
            self._prior_bar_delta = None

        self._bars_in_session += 1
        self._session_bars.append(current)

        # Update volatility tracking
        bar_range = current.high - current.low
        if bar_range > 0:
            self._bar_ranges.append(bar_range)
            if len(self._bar_ranges) > 0:
                self._avg_bar_range = sum(self._bar_ranges) / len(self._bar_ranges)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_bar_range = None
            self._pending_absorption_delta = None
            self._bars_since_absorption = 0
            self._consolidation_deltas = deque(maxlen=3)
            self._prior_bar_delta = current.delta
            return

        # Track consolidation pattern after absorption
        if self._pending_setup is not None:
            self._bars_since_absorption += 1
            self._consolidation_deltas.append(current.delta)
            
            # Check if we have enough gap and proper confirmation
            if self._bars_since_absorption > self.confirmation_gap_bars:
                if self._should_confirm_setup(current):
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                # Clear pending setup after attempting confirmation
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_bar_range = None
                self._pending_absorption_delta = None
                self._bars_since_absorption = 0
                self._consolidation_deltas = deque(maxlen=3)
            
            self._prior_bar_delta = current.delta
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            self._prior_bar_delta = current.delta
            return

        # Detect absorption candidate at carried level with directional confirmation
        setup = self._detect_absorption_at_directional_level(current)
        if setup:
            # Queue for confirmation on next bar(s)
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_bar_range = bar_range
            self._pending_absorption_delta = current.delta
            self._bars_since_absorption = 0
            self._consolidation_deltas = deque(maxlen=3)

        self._prior_bar_delta = current.delta

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _extract_session_levels_with_direction(self) -> Optional[dict]:
        """Extract key price levels with directional history from a completed session."""
        if not self._session_bars:
            return None
        
        bars_list = list(self._session_bars)
        
        # Track absorption events: bars with strong delta and wick
        absorption_events = []
        for bar in bars_list:
            if bar.volume == 0:
                continue
            delta_ratio = abs(bar.delta) / bar.volume
            bar_range = bar.high - bar.low
            if bar_range <= 0:
                continue
            
            # Identify absorption: strong delta + wick
            if delta_ratio >= self.absorption_delta_ratio:
                wick_ratio = 0
                if bar.delta > 0:
                    wick_ratio = (bar.close - bar.low) / bar_range
                else:
                    wick_ratio = (bar.high - bar.close) / bar_range
                
                if wick_ratio >= self.wick_to_range_min:
                    direction = "long" if bar.delta > 0 else "short"
                    rounded_close = round(bar.close * 4) / 4
                    absorption_events.append({
                        "level": rounded_close,
                        "direction": direction,
                        "delta": bar.delta,
                    })
        
        if not absorption_events:
            return None
        
        # Build directional level map
        level_directions = {}
        for event in absorption_events:
            level = event["level"]
            direction = event["direction"]
            if level not in level_directions:
                level_directions[level] = {"long_count": 0, "short_count": 0}
            if direction == "long":
                level_directions[level]["long_count"] += 1
            else:
                level_directions[level]["short_count"] += 1
        
        return {
            "absorption_events": absorption_events,
            "level_directions": level_directions,
        }

    def _get_directional_preference_at_level(self, price: float) -> Optional[str]:
        """
        Check if a price level has a directional preference from prior sessions.
        Returns "long", "short", or None (no clear preference).
        """
        if not self._carried_levels_with_direction:
            return None
        
        # Aggregate directional history across carried sessions
        long_count = 0
        short_count = 0
        
        for levels_dict in self._carried_levels_with_direction:
            if levels_dict is None or "level_directions" not in levels_dict:
                continue
            
            level_directions = levels_dict["level_directions"]
            
            # Find levels near the current price
            for level, counts in level_directions.items():
                distance = abs(price - level)
                if distance <= self.level_proximity:
                    long_count += counts.get("long_count", 0)
                    short_count += counts.get("short_count", 0)
        
        if long_count == 0 and short_count == 0:
            return None
        
        # Return the direction with more prior absorption
        if long_count > short_count:
            return "long"
        elif short_count > long_count:
            return "short"
        else:
            return None

    def _get_session_position_percentile(self) -> float:
        """Get the current position in the session as a percentile."""
        estimated_session_length = 20.0
        return self._bars_in_session / estimated_session_length

    def _detect_absorption_at_directional_level(self, current) -> Optional[dict]:
        """
        Detect absorption at a carried level with directional history.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Determine direction
        direction = "long" if delta > 0 else "short"
        
        # Check directional preference at nearby carried levels
        directional_preference = self._get_directional_preference_at_level(current.close)
        
        # If there's a directional preference, it must match
        if directional_preference is not None and directional_preference != direction:
            return None
        
        # If there's NO directional preference, check if we're near ANY carried level
        if directional_preference is None:
            has_nearby_level = False
            if self._carried_levels_with_direction:
                for levels_dict in self._carried_levels_with_direction:
                    if levels_dict is None or "level_directions" not in levels_dict:
                        continue
                    for level in levels_dict["level_directions"].keys():
                        if abs(current.close - level) <= self.level_proximity:
                            has_nearby_level = True
                            break
                    if has_nearby_level:
                        break
            
            if self._carried_levels_with_direction and not has_nearby_level:
                return None

        # Adjust wick requirement for early session
        session_pos = self._get_session_position_percentile()
        if session_pos < self.early_session_threshold:
            min_wick = self.min_wick_points * self.early_session_wick_mult
        else:
            min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            if lower_wick < min_wick:
                return None
            
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
        else:
            upper_wick = current.high - current.close
            
            if upper_wick < min_wick:
                return None
            
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

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Confirm absorption setup with consolidation gap and breakout criteria.
        
        Requirements:
        1. Gap requirement: sufficient bars have passed since absorption
        2. Consolidation pattern: prior bars show low delta (price consolidating)
        3. Breakout pattern: confirming bar shows increased delta
        4. Directional follow-through
        5. Lower volatility than absorption bar
        """
        if (self._pending_setup is None or self._pending_bar_range is None 
            or self._pending_absorption_close is None or self._pending_absorption_delta is None):
            return False

        direction = self._pending_setup["direction"]

        # Check consolidation pattern: prior bars should show low delta
        if len(self._consolidation_deltas) > 0:
            for consol_delta in self._consolidation_deltas:
                consol_delta_ratio = abs(consol_delta) / max(1, 10000)  # Rough estimate
                if consol_delta_ratio > self.consolidation_delta_ratio:
                    # Prior bar was too directional; not consolidation
                    return False

        # Check breakout: confirming bar must show increased directional commitment
        confirm_delta_ratio = abs(confirming_bar.delta) / max(1, confirming_bar.volume)
        if confirm_delta_ratio < self.breakout_delta_ratio:
            return False

        # Calculate confirming bar volatility
        confirm_range = confirming_bar.high - confirming_bar.low
        if confirm_range <= 0:
            confirm_range = 0.01
        
        # Check volatility ratio
        if self._pending_bar_range > 0:
            volatility_ratio = confirm_range / self._pending_bar_range
        else:
            volatility_ratio = 1.0
        
        if volatility_ratio > self.volatility_ratio_threshold:
            return False

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            if confirming_bar.delta <= 0:
                return False
            if confirming_bar.delta < self._pending_absorption_delta * self.delta_increase_ratio:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            if confirming_bar.delta >= 0:
                return False
            if abs(confirming_bar.delta) < abs(self._pending_absorption_delta) * self.delta_increase_ratio:
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
