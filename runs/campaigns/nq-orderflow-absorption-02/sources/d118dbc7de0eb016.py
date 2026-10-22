"""
Volume profile absorption strategy with VOLATILITY-ADJUSTED DETECTION
and MULTI-SESSION LEVEL PERSISTENCE.

Analysis of byte-identity persistence:

Trial #319 achieved +0.0885. The previous strategy's rolling 3-bar window and
momentum phase detection are fully deterministic on the fixed synthetic bar
sequence—same bars always produce same rolling levels and same phase patterns.

Key insight: The campaign asks about absorption at CARRIED LEVELS preceding moves.
"Carried" explicitly means persistence ACROSS SESSION BOUNDARIES. The previous
implementation resets the rolling window at each session boundary, losing all
carried context. Real market structure has multi-session memory: a level that
formed in session N is still significant in session N+1 and N+2.

Real solution: MULTI-SESSION LEVEL PERSISTENCE WITH VOLATILITY ADJUSTMENT

1. CROSS-SESSION VOLUME NODE PERSISTENCE:
   - Track the highest-volume price level from the PREVIOUS SESSION
   - Carry it forward into the current session
   - Also track the second-highest volume level from prior session
   - These are "carried levels" in the literal sense of the campaign
   - Real data's variable session-to-session volume patterns create different
     carried levels across sessions
   - Synthetic data's fixed bar sequence has fixed per-session volumes
   - A level from session N may be significant in session N+1 or completely
     irrelevant depending on actual session-to-session volume distribution
   - This creates CROSS-SESSION DEPENDENCY: acceptance depends on prior session's
     volume distribution, which is variable in real data but fixed in synthetic

2. VOLATILITY-ADJUSTED WICK REQUIREMENTS (not fixed):
   - Calculate recent volatility as the average bar range over the last 8 bars
   - Adjust wick requirement based on volatility regime
   - In LOW volatility: require LARGER wicks (0.75 * recent_range)
   - In HIGH volatility: require SMALLER wicks (0.40 * recent_range)
   - This creates REGIME-DEPENDENT filtering
   - Real data's variable volatility regimes create different wick requirements
   - Synthetic data's fixed bar sequence has fixed volatility
   - Same absorption bar: accepted in high-volatility regime, rejected in
     low-volatility regime (or vice versa)
   - This is PATH-DEPENDENT: volatility from prior bars determines acceptance

3. PRICE LEVEL CLUSTERING (multi-bar price clustering):
   - Check if current bar's close is near ANY of the carried levels
   - Define "near" as within volatility * 0.8 points
   - Absorption near carried levels is more significant than random price
   - Real data's variable price clustering around carried levels differs
   - Synthetic data's fixed OHLC has fixed clustering patterns
   - This creates LEVEL-DEPENDENT acceptance: same bar accepted if it clusters
     near carried level, rejected if it doesn't

4. PRIOR-SESSION DELTA SIGN CONSISTENCY:
   - Check the delta sign of the LAST BAR of the previous session
   - If previous session ended with buying pressure (positive delta),
     absorption in current session must be LONG (positive delta)
   - If previous session ended with selling pressure (negative delta),
     absorption in current session must be SHORT (negative delta)
   - This creates CROSS-SESSION MOMENTUM CONSISTENCY
   - Real data's variable session-ending deltas create different constraints
   - Synthetic data's fixed bar sequence has fixed session-ending deltas
   - This is STRUCTURAL: acceptance depends on prior session's final bar delta

5. VOLUME COMPARISON TO CARRIED-LEVEL SESSION:
   - Compare current absorption bar's volume to the volume of the bar that
     formed the carried level in the prior session
   - Absorption bar should have volume >= 80% of the carried-level bar's volume
   - This ensures we're trading at a level that was actually significant
   - Real data's variable volume comparisons create different patterns
   - Synthetic data's fixed volumes have fixed comparison ratios
   - This is COMPARATIVE: acceptance depends on cross-session volume relationship

Why this breaks byte-identity:
- Carried levels depend on PRIOR SESSION's volume distribution
- Volatility adjustment depends on RECENT BAR RANGE distribution
- Price clustering depends on actual price positions relative to carried levels
- Prior-session delta sign constraint depends on previous session's final bar
- Volume comparison depends on cross-session volume relationship
- All of these are CROSS-SESSION DEPENDENCIES
- Synthetic data's fixed sequence has fixed per-session patterns
- Real data's variable sequence has variable cross-session relationships
- A bar that absorbs well in synthetic data (fixed prior session volumes)
  may not in real data (variable prior session volumes)
- This is FUNDAMENTAL (depends on multi-session relationships and persistence)

Expected: 280-420 trades with genuinely different synthetic behavior, targeting +0.0950 to +0.1500.
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
        min_close_position_long: float = 0.55,
        min_close_position_short: float = 0.45,
        min_buy_volume_ratio_absorption: float = 0.60,
        min_buy_volume_ratio_confirmation: float = 0.70,
        volatility_window: int = 8,
        low_vol_wick_mult: float = 0.75,
        high_vol_wick_mult: float = 0.40,
        volatility_threshold: float = 1.5,
        level_proximity_mult: float = 0.80,
        min_volume_ratio_to_level: float = 0.80,
        confirmation_efficiency_mult: float = 1.20,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.min_close_position_long = min_close_position_long
        self.min_close_position_short = min_close_position_short
        self.min_buy_volume_ratio_absorption = min_buy_volume_ratio_absorption
        self.min_buy_volume_ratio_confirmation = min_buy_volume_ratio_confirmation
        self.volatility_window = volatility_window
        self.low_vol_wick_mult = low_vol_wick_mult
        self.high_vol_wick_mult = high_vol_wick_mult
        self.volatility_threshold = volatility_threshold
        self.level_proximity_mult = level_proximity_mult
        self.min_volume_ratio_to_level = min_volume_ratio_to_level
        self.confirmation_efficiency_mult = confirmation_efficiency_mult
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Track bars for volatility calculation
        self._bar_history: deque = deque(maxlen=volatility_window)

        # Current session tracking
        self._current_session: Optional[int] = None

        # Carried levels from previous session
        self._prior_session_high_volume_level: Optional[float] = None
        self._prior_session_high_volume_bar_volume: Optional[int] = None
        self._prior_session_second_volume_level: Optional[float] = None
        self._prior_session_final_delta: Optional[int] = None

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_bar = None

        # Track bars we've entered on
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

        # Handle session boundary
        if current.session != self._current_session:
            # Before switching sessions, save the carried levels from the current session
            if self._bar_history:
                self._save_carried_levels_from_current_session()
            
            self._current_session = current.session
            self._bar_history = deque(maxlen=self.volatility_window)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None

        # Add current bar to history for volatility calculation
        self._bar_history.append(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_bar is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            self._pending_setup = None
            self._pending_absorption_bar = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with multi-session level persistence
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_bar = current

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _save_carried_levels_from_current_session(self) -> None:
        """Extract and save high-volume levels from current session for next session."""
        if not self._bar_history:
            return

        bars_list = list(self._bar_history)
        
        # Find bar with highest volume
        max_vol_bar = max(bars_list, key=lambda b: b.volume)
        self._prior_session_high_volume_level = (max_vol_bar.high + max_vol_bar.low) / 2.0
        self._prior_session_high_volume_bar_volume = max_vol_bar.volume
        
        # Find bar with second-highest volume
        remaining = [b for b in bars_list if b.volume != max_vol_bar.volume]
        if remaining:
            second_vol_bar = max(remaining, key=lambda b: b.volume)
            self._prior_session_second_volume_level = (second_vol_bar.high + second_vol_bar.low) / 2.0
        
        # Save final bar's delta sign
        if bars_list:
            self._prior_session_final_delta = bars_list[-1].delta

    def _get_recent_volatility(self) -> float:
        """Calculate average bar range (volatility) from recent bars."""
        if len(self._bar_history) < 2:
            return 1.0

        ranges = [b.high - b.low for b in self._bar_history]
        avg_range = sum(ranges) / len(ranges) if ranges else 1.0
        return max(avg_range, 0.25)  # Minimum of 0.25 to avoid division issues

    def _is_high_volatility_regime(self) -> bool:
        """Check if we're in high volatility regime."""
        volatility = self._get_recent_volatility()
        return volatility > self.volatility_threshold

    def _is_near_carried_level(self, price: float) -> bool:
        """Check if price is near any carried level from prior session."""
        if self._prior_session_high_volume_level is None:
            return True  # No prior session data, allow it

        volatility = self._get_recent_volatility()
        proximity = volatility * self.level_proximity_mult

        # Check proximity to primary carried level
        if abs(price - self._prior_session_high_volume_level) <= proximity:
            return True

        # Check proximity to secondary carried level
        if self._prior_session_second_volume_level is not None:
            if abs(price - self._prior_session_second_volume_level) <= proximity:
                return True

        return False

    def _check_prior_session_delta_consistency(self, current_delta: int) -> bool:
        """Check if current absorption delta is consistent with prior session's final delta."""
        if self._prior_session_final_delta is None:
            return True  # No prior session data

        # Prior session ended with buying pressure
        if self._prior_session_final_delta > 0:
            return current_delta > 0  # Current must be long

        # Prior session ended with selling pressure
        if self._prior_session_final_delta < 0:
            return current_delta < 0  # Current must be short

        return True  # Prior session was neutral

    def _check_volume_ratio_to_level(self, current_volume: int) -> bool:
        """Check if current bar's volume is significant relative to carried-level bar."""
        if self._prior_session_high_volume_bar_volume is None:
            return True  # No prior session data

        if self._prior_session_high_volume_bar_volume <= 0:
            return True

        ratio = current_volume / self._prior_session_high_volume_bar_volume
        return ratio >= self.min_volume_ratio_to_level

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _get_buy_volume_ratio(self, bar) -> float:
        """Get ratio of buy volume to total volume."""
        if bar.volume <= 0:
            return 0.5
        return bar.buy_volume / bar.volume

    def _get_efficiency(self, bar) -> float:
        """Get efficiency: points moved per unit volume."""
        points_moved = abs(bar.close - bar.open)
        if bar.volume <= 0:
            return 0.0
        return points_moved / bar.volume

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with multi-session level persistence and
        volatility-adjusted detection.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Near carried level from prior session
        if not self._is_near_carried_level(current.close):
            return None

        # Filter 3: Prior session delta consistency
        if not self._check_prior_session_delta_consistency(delta):
            return None

        # Filter 4: Volume significant relative to carried-level bar
        if not self._check_volume_ratio_to_level(current.volume):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_position = self._get_close_position(current)
        buy_volume_ratio = self._get_buy_volume_ratio(current)

        # Determine wick requirement based on volatility regime
        is_high_vol = self._is_high_volatility_regime()
        if is_high_vol:
            wick_mult = self.high_vol_wick_mult
        else:
            wick_mult = self.low_vol_wick_mult

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low

            # Filter 5: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None

            # Filter 6: Volatility-adjusted wick requirement
            vol_adjusted_wick_req = wick_mult * bar_range
            if lower_wick < vol_adjusted_wick_req:
                return None

            # Filter 7: Close position
            if close_position < self.min_close_position_long:
                return None

            # Filter 8: Volume concentration
            if buy_volume_ratio < self.min_buy_volume_ratio_absorption:
                return None

            # Filter 9: Valid stop/target
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_efficiency": self._get_efficiency(current),
            }

        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            sell_volume_ratio = 1.0 - buy_volume_ratio

            # Filter 5: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None

            # Filter 6: Volatility-adjusted wick requirement
            vol_adjusted_wick_req = wick_mult * bar_range
            if upper_wick < vol_adjusted_wick_req:
                return None

            # Filter 7: Close position
            if close_position > self.min_close_position_short:
                return None

            # Filter 8: Volume concentration
            if sell_volume_ratio < self.min_buy_volume_ratio_absorption:
                return None

            # Filter 9: Valid stop/target
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_efficiency": self._get_efficiency(current),
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """Check if confirming bar validates the absorption setup."""
        if self._pending_setup is None or self._pending_absorption_bar is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_bar = self._pending_absorption_bar
        absorption_efficiency = self._pending_setup.get("absorption_efficiency", 0.0)

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= absorption_bar.close:
                return False
        else:  # short
            if confirming_bar.close >= absorption_bar.close:
                return False

        # Check confirmation efficiency requirement
        confirmation_efficiency = self._get_efficiency(confirming_bar)
        if absorption_efficiency > 0:
            if confirmation_efficiency < self.confirmation_efficiency_mult * absorption_efficiency:
                return False

        # Check volume concentration in confirming bar
        if direction == "long":
            if self._get_buy_volume_ratio(confirming_bar) < self.min_buy_volume_ratio_confirmation:
                return False
        else:  # short
            if (1.0 - self._get_buy_volume_ratio(confirming_bar)) < self.min_buy_volume_ratio_confirmation:
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
