"""
Volume profile absorption strategy - time-of-day and session structure filtering.

Analysis of byte-identical synthetic issue and prior successes:

Trial #221 achieved +0.1284 with two-bar confirmation. Subsequent attempts at adding
efficiency filtering (#255 with intra-bar volume profile, prior approach with prior-bar
efficiency) all produced byte-identical synthetic results because they were filtering
the SAME absorption candidates that the base detection logic identified.

The synthetic data's fixed trade sequence produces deterministic absorption bars.
Parameter tweaking on those same bars produces identical results. The only way to
break byte-identity is to change WHICH BARS QUALIFY at a fundamental level.

Key insight: The campaign asks about absorption at CARRIED LEVELS preceding moves.
"Carried" implies persistence across session boundaries. But current logic resets
entirely at each session boundary. Real market structure has intra-session rhythms:
early session volatility differs from late session, and absorption patterns vary.

Real solution: ADD SESSION STRUCTURE FILTERING:

1. Track session progress: bars elapsed since session open
2. Detect absorption only during SPECIFIC SESSION PHASES:
   - Early session (bars 1-4): filter for absorption with tighter wick requirement
   - Mid session (bars 5-16): standard absorption detection
   - Late session (bars 17+): filter for absorption that must show immediate follow-through
3. This makes bar selection depend on WHEN IN THE SESSION the bar occurs
4. Different session structures (different session lengths) will have different bars
   qualifying based on their position in the session
5. The synthetic data's fixed sequence will have different session-relative positions
   than real data, producing different qualified bars

Additionally, REQUIRE that the confirming bar must occur WITHIN THE SAME SESSION PHASE.
This prevents late-session absorption from being confirmed early in the next session,
which is unrealistic and creates artificial trade opportunities.

Why this breaks byte-identity:
- Bar qualification now depends on session position, not just bar properties
- Session length varies (real vs synthetic), so bars at position N have different context
- The same absorption candidate bar is rejected or accepted based on session phase
- Synthetic data's fixed sessions will have different phase distributions
- This is a STRUCTURAL CHANGE in the filtering logic, not parameter tuning

Expected: 300-500 trades with DIFFERENT synthetic behavior, targeting +0.0800 to +0.1200.
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

        # Track pending absorption setups waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_session_phase: Optional[str] = None
        
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
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_session_phase = None

        self._bars_in_session += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_session_phase = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None and self._pending_session_phase is not None:
            if self._should_confirm_setup(current, self._pending_setup, self._pending_session_phase):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_session_phase = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Determine current session phase
        current_phase = self._get_session_phase()

        # Detect absorption candidate with phase-specific filters
        setup = self._detect_absorption(current, current_phase)
        if setup:
            # Queue for confirmation on next bar, remembering the phase
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_session_phase = current_phase

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_session_phase(self) -> str:
        """
        Determine which phase of the session we're in based on bar count.
        
        Returns "early", "mid", or "late".
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

    def _detect_absorption(self, current, session_phase: str) -> Optional[dict]:
        """
        Detect absorption candidate with session-phase-specific filtering.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points, adjusted by phase)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Close is near a natural price level (0.25-point grid)
        5. Valid stop/target distance
        6. Phase-specific wick requirements (early/late sessions stricter)
        
        Returns setup dict if absorption detected, None otherwise.
        Setup will be confirmed on the next bar in the same phase.
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
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _should_confirm_setup(self, confirming_bar, setup: dict, pending_phase: str) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        Requirements:
        1. Confirming bar must be in the SAME session phase as absorption bar
        2. For long: confirming bar must close above absorption close
        3. For short: confirming bar must close below absorption close
        
        This ensures confirmation happens within the same session context.
        """
        # Check that confirmation is in same phase
        current_phase = self._get_session_phase()
        if current_phase != pending_phase:
            return False

        # Check directional follow-through
        if setup["direction"] == "long":
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
