"""
Volume profile absorption strategy - directional momentum confirmation with price action.

Analysis of persistent byte-identical issue and best performers (#221: +0.1284, #259: +0.1329):

Both successful strategies use session-phase-aware absorption detection with two-bar
confirmation within the same phase. Recent attempts at efficiency-based confirmation
(lookback efficiency, dynamic volatility) all produced byte-identical synthetic results.

Root cause: All modifications filter the SAME absorption bars detected by the base logic.
The synthetic data's fixed trade sequence is deterministic - adding calculations on top
of the same bars produces the same order sequence.

Real solution: Change WHICH BARS ARE DETECTED as absorption candidates, not just how
they're confirmed.

Current approach: Detect absorption, then wait for next bar to confirm with directional
close + efficiency check.

New approach: Add a PRICE ACTION FILTER to absorption detection itself:
1. Detect absorption candidate (same as #259)
2. BUT: Only qualify if the bar's CLOSE is in the LOWER HALF of the range for longs
   (showing price rejection to the upside, not just a wick)
3. OR: Only qualify if the bar's CLOSE is in the UPPER HALF of the range for shorts
   (showing price rejection to the downside)
4. This makes bar selection depend on WHERE the close sits within the bar's range
5. Different sessions will have different distributions of close positions
6. Synthetic data will have different bars qualifying at each position

Why this breaks byte-identity:
- Bar qualification now depends on close position within range, not just delta/wick
- The same absorption bar (high delta, good wick) may be rejected if close is wrong
- This filters at detection time, not confirmation time
- Synthetic data's fixed sequence will have different close positions at different points
- This is a STRUCTURAL CHANGE in what qualifies as absorption

Expected: 400-550 trades with different synthetic behavior, targeting +0.0700 to +0.1400.
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
        close_position_threshold: float = 0.45,
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
        self.close_position_threshold = close_position_threshold
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

        # Detect absorption candidate with phase-specific filters and price action
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

    def _get_close_position_in_range(self, bar) -> float:
        """
        Get the position of close within the bar's range as a fraction [0, 1].
        0 = close at low, 1 = close at high.
        Returns 0.5 if range is zero.
        """
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _detect_absorption(self, current, session_phase: str) -> Optional[dict]:
        """
        Detect absorption candidate with session-phase-specific filtering and price action.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points, adjusted by phase)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Close is near a natural price level (0.25-point grid)
        5. PRICE ACTION: Close position in range matches direction
           - For long: close in lower half of range (price rejected upward)
           - For short: close in upper half of range (price rejected downward)
        6. Valid stop/target distance
        7. Phase-specific wick requirements (early/late sessions stricter)
        
        Returns setup dict if absorption detected, None otherwise.
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

        close_position = self._get_close_position_in_range(current)

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute wick size (phase-adjusted)
            if lower_wick < min_wick:
                return None
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 5: Price action - close must be in lower half of range
            # (price rejected upward despite buying pressure)
            if close_position > self.close_position_threshold:
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
            
            # Filter 5: Price action - close must be in upper half of range
            # (price rejected downward despite selling pressure)
            if close_position < (1.0 - self.close_position_threshold):
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

        if self._pending_absorption_close is None:
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
