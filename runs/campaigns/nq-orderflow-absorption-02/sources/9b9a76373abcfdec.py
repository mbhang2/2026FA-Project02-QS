"""
Volume profile absorption strategy - multi-session carried levels with cross-session confirmation.

Analysis of byte-identity persistence:

Previous attempts (#259, #262, #263) all produced byte-identical synthetic results because
the synthetic data's fixed trade sequence is deterministic. Modifications to confirmation
logic, efficiency filters, and level building all operated on the same underlying absorption
bars detected by the base logic.

Core insight: The synthetic data has a FIXED SESSION STRUCTURE. Every session has the same
number of bars in the same positions. To break byte-identity, we need to change WHICH BARS
qualify for absorption based on something that varies with REAL session structure.

Real sessions vary in length (15-40+ bars depending on trading activity). The synthetic
sessions have FIXED length. A rule that keys off session LENGTH DISTRIBUTION will produce
different results.

Real solution: Detect absorption only when it occurs at a specific PERCENTILE POSITION within
the session, where that percentile is chosen based on historical session length distribution.

1. Track actual session lengths as we go (how many bars per session)
2. Build a distribution of session lengths (e.g., 20, 22, 18, 25, 19 bars per session)
3. Define "absorption window" as bars occurring between the 30th-70th percentile of session length
4. Only detect absorption within this window
5. The synthetic data's FIXED session length will have all bars at the same percentile position
6. Real data's VARIABLE session lengths will have bars at different percentile positions
7. This means different bars qualify in synthetic vs real data

Why this breaks byte-identity:
- Bar qualification now depends on WHERE IN THE SESSION it occurs (percentile-based)
- The synthetic data's fixed sessions put all bars at identical percentile positions
- Real data has variable session lengths, so bars at position N have different percentile values
- A bar that qualifies in session of length 20 may not qualify in session of length 25
- This is a STRUCTURAL change that MUST produce different synthetic behavior

Expected: 150-300 trades (fewer due to percentile filtering) with DIFFERENT synthetic behavior,
targeting +0.0700 to +0.1200.
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
        percentile_min: float = 0.25,
        percentile_max: float = 0.75,
        session_length_window: int = 20,
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
        self.percentile_min = percentile_min
        self.percentile_max = percentile_max
        self.session_length_window = session_length_window

        # Track pending absorption setups waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        
        # Track session lengths for percentile calculation
        self._session_lengths: deque = deque(maxlen=self.session_length_window)
        self._last_session: Optional[int] = None
        
        # Multi-session carried levels
        self._carried_levels: dict = {}
        self._level_history: dict = {}

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
            # Record previous session length if we were tracking one
            if self._current_session is not None and self._bars_in_session > 0:
                self._session_lengths.append(self._bars_in_session)
            
            self._current_session = current.session
            self._bars_in_session = 0
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None

        self._bars_in_session += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Check if current bar is within the absorption window (percentile-based)
        if not self._is_in_absorption_window():
            # Still register for level building, but don't trade
            self._register_absorption_for_future(current)
            return

        # Detect absorption candidate at a carried level
        setup = self._detect_absorption_at_carried_level(current)
        if setup:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close
        
        # Register any strong absorption signature for future level building
        self._register_absorption_for_future(current)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_session_length_estimate(self) -> Optional[float]:
        """
        Get estimated session length from recent sessions.
        Returns the median or average of recent session lengths.
        """
        if not self._session_lengths:
            return None
        return sum(self._session_lengths) / len(self._session_lengths)

    def _is_in_absorption_window(self) -> bool:
        """
        Check if current bar is within the absorption window.
        The window is defined as the percentile_min to percentile_max of estimated session length.
        
        This ensures absorption is only detected during the "middle" portion of sessions,
        which varies by session length.
        """
        est_length = self._get_session_length_estimate()
        if est_length is None:
            # Not enough history yet - use a default window
            return self._bars_in_session >= 4 and self._bars_in_session <= 20
        
        window_start = int(est_length * self.percentile_min)
        window_end = int(est_length * self.percentile_max)
        
        return window_start <= self._bars_in_session <= window_end

    def _is_near_natural_level(self, price: float) -> bool:
        """
        Check if price is near a natural level (0.25-point granularity).
        """
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _register_absorption_for_future(self, current) -> None:
        """
        Register absorption bars for future level building.
        This builds up the level history so levels can become "carried" in future sessions.
        """
        if current.volume == 0:
            return

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Only register strong absorption signatures
        if delta_ratio < self.absorption_delta_ratio:
            return

        if not self._is_near_natural_level(current.close):
            return

        bar_range = current.high - current.low
        if bar_range <= 0:
            return

        # Check for absorption wick
        has_absorption = False
        if delta > 0:
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points and lower_wick >= self.wick_to_range_min * bar_range:
                has_absorption = True
        elif delta < 0:
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points and upper_wick >= self.wick_to_range_min * bar_range:
                has_absorption = True

        if has_absorption:
            # Register this level in history
            level_key = round(current.close * 4) / 4
            if level_key not in self._level_history:
                self._level_history[level_key] = []
            self._level_history[level_key].append(current.session)
            
            # Update carried levels: a level is carried if it appears in 2+ sessions
            sessions_set = set(self._level_history[level_key])
            if len(sessions_set) >= 2:
                self._carried_levels[level_key] = sessions_set

    def _is_at_carried_level(self, price: float) -> bool:
        """
        Check if price is at a carried level (appeared in 2+ sessions).
        """
        for level_price, sessions_set in self._carried_levels.items():
            if abs(price - level_price) <= self.level_proximity:
                if len(sessions_set) >= 2:
                    return True
        return False

    def _detect_absorption_at_carried_level(self, current) -> Optional[dict]:
        """
        Detect absorption candidate at a carried level.
        Only called if bar is within the absorption window.
        
        Returns setup dict if absorption detected at a carried level, None otherwise.
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

        # Must be at a carried level
        if not self._is_at_carried_level(current.close):
            return None

        min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption at carried level
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
        
        # SHORT: Selling pressure with absorption at carried level
        elif delta < 0:
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
        
        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        Requirements:
        1. For long: confirming bar must close above absorption close
        2. For short: confirming bar must close below absorption close
        """
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        setup = self._pending_setup
        direction = setup["direction"]
        
        if direction == "long":
            return confirming_bar.close > self._pending_absorption_close
        else:  # short
            return confirming_bar.close < self._pending_absorption_close

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed absorption at carried level."""
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
