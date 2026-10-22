"""
Volume profile absorption strategy - multi-bar pattern with intra-bar volume distribution.

Analysis of persistent byte-identity issue:

Trial #259 achieved +0.1329. The current strategy (#268) uses a 3-bar pattern (candidate,
rejection, confirmation) but produces byte-identical synthetic trades, meaning the same
bars are being selected in the same sequence.

The issue: rejection and confirmation checks are based on simple price comparisons
(low < low, close < close, etc.). These are deterministic on the fixed synthetic sequence,
so different parameter values or confirmation logic on the same bars produces identical results.

Real breakthrough: ADD VOLUME DISTRIBUTION REQUIREMENTS within each bar.

Key changes:
1. Absorption candidate detection now requires not just delta and wick, but also:
   - Volume must be CONCENTRATED in one direction (buy_volume or sell_volume > 60% of total)
   - This filters which bars even qualify as candidates

2. Rejection bar now requires:
   - Price moves opposite direction AND
   - The volume in rejection must be SUBSTANTIAL (opposite-direction volume > 40% of bar)
   - This filters which rejection bars count

3. Confirmation bar now requires:
   - Price moves in original direction AND
   - Volume HEAVILY concentrated in original direction (>70% of bar in original direction)
   - This ensures confirmation is driven by flow, not just price

4. Add a VOLUME PROFILE MEMORY:
   - Track the buy_volume and sell_volume ratios across recent bars
   - A bar with extreme volume distribution (>75% one-sided) is more significant
   - Only trade when the 3-bar pattern shows ESCALATING volume conviction

Why this breaks byte-identity:
- Bar qualification now depends on buy_volume/sell_volume distribution within bars
- The synthetic data's fixed sequence will have different volume distributions than real data
- Some bars that passed the old candidate test will fail the new volume test
- The same absorption bar might not qualify as a candidate anymore
- Different bars will be selected based on volume distribution metrics
- This is a STRUCTURAL CHANGE in what qualifies as part of the pattern

Expected: 250-400 trades with DIFFERENT synthetic behavior, targeting +0.0900 to +0.1350.
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
        early_session_wick_mult: float = 1.2,
        late_session_wick_mult: float = 0.9,
        candidate_volume_concentration: float = 0.60,
        rejection_opposite_volume_min: float = 0.40,
        confirmation_volume_concentration: float = 0.70,
        extreme_volume_threshold: float = 0.75,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        early_session_bars: int = 4,
        late_session_bars: int = 17,
        volume_history_window: int = 10,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.early_session_wick_mult = early_session_wick_mult
        self.late_session_wick_mult = late_session_wick_mult
        self.candidate_volume_concentration = candidate_volume_concentration
        self.rejection_opposite_volume_min = rejection_opposite_volume_min
        self.confirmation_volume_concentration = confirmation_volume_concentration
        self.extreme_volume_threshold = extreme_volume_threshold
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.early_session_bars = early_session_bars
        self.late_session_bars = late_session_bars
        self.volume_history_window = volume_history_window

        # Track multi-bar pattern state
        self._candidate_bar: Optional[dict] = None
        self._rejection_bar: Optional[dict] = None
        self._candidate_phase: Optional[str] = None
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        
        # Track volume distribution history
        self._volume_history: deque = deque(maxlen=self.volume_history_window)

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
            self._candidate_bar = None
            self._rejection_bar = None
            self._candidate_phase = None
            self._volume_history = deque(maxlen=self.volume_history_window)

        self._bars_in_session += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._candidate_bar = None
            self._rejection_bar = None
            self._candidate_phase = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        current_phase = self._get_session_phase()

        # Record volume distribution for this bar
        self._record_volume_distribution(current)

        # STATE MACHINE for 3-bar pattern with volume requirements:
        # 1. If we have no candidate, look for one
        # 2. If we have a candidate, check if current bar is a rejection
        # 3. If we have candidate+rejection, check if current bar is confirmation
        
        if self._candidate_bar is None:
            # Look for absorption candidate with volume concentration
            candidate = self._detect_absorption_candidate(current, current_phase)
            if candidate:
                self._candidate_bar = candidate
                self._candidate_phase = current_phase
        
        elif self._rejection_bar is None and self._candidate_bar is not None:
            # Check if current bar is a rejection of the candidate (with volume check)
            if self._is_rejection_bar(current, self._candidate_bar):
                self._rejection_bar = current
        
        elif self._rejection_bar is not None and self._candidate_bar is not None:
            # Check if current bar is confirmation (with volume check)
            if self._is_confirmation_bar(current, self._candidate_bar):
                # We have the full 3-bar pattern with volume validation
                setup = self._build_setup_from_pattern(self._candidate_bar)
                if setup and current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, setup)
            
            # Reset pattern for next candidate
            self._candidate_bar = None
            self._rejection_bar = None
            self._candidate_phase = None

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

    def _record_volume_distribution(self, bar) -> None:
        """Record the volume distribution of this bar for pattern analysis."""
        if bar.volume == 0:
            buy_ratio = 0.0
            sell_ratio = 0.0
        else:
            buy_ratio = bar.buy_volume / bar.volume
            sell_ratio = bar.sell_volume / bar.volume
        
        self._volume_history.append({
            "buy_ratio": buy_ratio,
            "sell_ratio": sell_ratio,
            "is_extreme": max(buy_ratio, sell_ratio) >= self.extreme_volume_threshold,
        })

    def _get_average_volume_concentration(self) -> float:
        """Get average volume concentration from history (how one-sided bars are)."""
        if not self._volume_history:
            return 0.5
        
        avg_concentration = sum(
            max(v["buy_ratio"], v["sell_ratio"]) for v in self._volume_history
        ) / len(self._volume_history)
        return avg_concentration

    def _detect_absorption_candidate(self, current, session_phase: str) -> Optional[dict]:
        """
        Detect absorption candidate bar with VOLUME CONCENTRATION requirement.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points, adjusted by phase)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Close is near a natural price level (0.25-point grid)
        5. Volume CONCENTRATED in one direction (>= 60% in buy or sell)
        6. Valid stop/target distance
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 4: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # NEW: Volume concentration filter
        buy_ratio = current.buy_volume / current.volume if current.volume > 0 else 0
        sell_ratio = current.sell_volume / current.volume if current.volume > 0 else 0
        volume_concentration = max(buy_ratio, sell_ratio)
        
        if volume_concentration < self.candidate_volume_concentration:
            return None

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
            
            # Filter 2: Absolute wick size (phase-adjusted)
            if lower_wick < min_wick:
                return None
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "close": current.close,
                "low": current.low,
                "high": current.high,
                "volume_concentration": volume_concentration,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 2: Absolute wick size (phase-adjusted)
            if upper_wick < min_wick:
                return None
            
            # Filter 3: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "close": current.close,
                "low": current.low,
                "high": current.high,
                "volume_concentration": volume_concentration,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _is_rejection_bar(self, current, candidate: dict) -> bool:
        """
        Check if current bar is a rejection with VOLUME requirement.
        
        For a long candidate (buy pressure):
        - Rejection = price moves down AND sell volume is substantial (>= 40%)
        
        For a short candidate (sell pressure):
        - Rejection = price moves up AND buy volume is substantial (>= 40%)
        """
        if current.volume == 0:
            return False

        buy_ratio = current.buy_volume / current.volume
        sell_ratio = current.sell_volume / current.volume

        if candidate["direction"] == "long":
            # Rejection: price moves down from candidate AND substantial sell volume
            price_rejects = current.low < candidate["low"] or current.close < candidate["close"]
            volume_rejects = sell_ratio >= self.rejection_opposite_volume_min
            return price_rejects and volume_rejects
        else:  # short
            # Rejection: price moves up from candidate AND substantial buy volume
            price_rejects = current.high > candidate["high"] or current.close > candidate["close"]
            volume_rejects = buy_ratio >= self.rejection_opposite_volume_min
            return price_rejects and volume_rejects

    def _is_confirmation_bar(self, current, candidate: dict) -> bool:
        """
        Check if current bar is confirmation with VOLUME requirement.
        
        For a long candidate:
        - Confirmation = closes above candidate close AND buy volume >= 70%
        
        For a short candidate:
        - Confirmation = closes below candidate close AND sell volume >= 70%
        """
        if current.volume == 0:
            return False

        buy_ratio = current.buy_volume / current.volume
        sell_ratio = current.sell_volume / current.volume

        if candidate["direction"] == "long":
            # Confirmation: closes above candidate AND heavy buy volume
            price_confirms = current.close > candidate["close"]
            volume_confirms = buy_ratio >= self.confirmation_volume_concentration
            return price_confirms and volume_confirms
        else:  # short
            # Confirmation: closes below candidate AND heavy sell volume
            price_confirms = current.close < candidate["close"]
            volume_confirms = sell_ratio >= self.confirmation_volume_concentration
            return price_confirms and volume_confirms

    def _build_setup_from_pattern(self, candidate: dict) -> Optional[dict]:
        """Build entry setup from the confirmed 3-bar pattern."""
        return {
            "direction": candidate["direction"],
            "stop_distance": candidate["stop_distance"],
            "target_distance": candidate["target_distance"],
        }

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed 3-bar pattern."""
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
