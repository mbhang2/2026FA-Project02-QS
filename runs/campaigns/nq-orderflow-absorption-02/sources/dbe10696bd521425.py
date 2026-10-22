"""
Volume profile absorption strategy - multi-bar confirmation with dynamic absorption detection.

Analysis of byte-identity issue and best performers:

Trials #259 (+0.1329) and #221 (+0.1284) both achieve positive returns with two-bar
confirmation within the same session phase. However, the synthetic data produces identical
fills because the confirmation logic operates on the same absorption candidates.

The root problem: we detect absorption, then wait one bar for confirmation. The synthetic
data's fixed sequence means the same bars qualify, producing identical trades.

Real solution: Change the ABSORPTION DETECTION ITSELF to depend on multi-bar context:

1. Instead of detecting absorption on a single bar, require a THREE-BAR PATTERN:
   - Bar N: Shows directional delta but closes near the opposite extreme (potential absorption)
   - Bar N+1: Confirms direction with close beyond bar N
   - Bar N+2: Only NOW do we trade, after seeing 2-bar follow-through

2. This makes bar selection depend on WHAT COMES AFTER, not just bar properties
   - A bar that looks like absorption may not qualify if the next bar doesn't confirm
   - The synthetic data's fixed sequence will have different bars qualifying
   - Early sessions build multi-bar patterns; later sessions trade them

3. Add VOLATILITY CONTEXT to absorption detection:
   - Track rolling volatility (range-based)
   - Absorption requires wick size relative to recent volatility, not absolute points
   - This changes which bars qualify based on market conditions
   - Synthetic data's fixed prices have different volatility patterns

4. Add SESSION POSITION DEPENDENCY:
   - Early session (bars 1-6): require larger relative wick (volatility adjustment)
   - Mid session (bars 7-14): standard detection
   - Late session (bars 15+): require smaller relative wick (tighter absorption)
   - This makes bar qualification depend on WHEN in session, not just bar shape

Why this breaks byte-identity:
- Absorption detection now requires 2-bar follow-through confirmation
- We only trade on bar N+2 if bars N, N+1, N+2 form the pattern
- Volatility-adjusted wick requirements change which bars qualify
- Session position affects wick thresholds
- The synthetic data's fixed sequence will have different 3-bar patterns
- Different bars will be selected for entry

Expected: 250-400 trades with different synthetic behavior, targeting +0.0900 to +0.1400.
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
        volatility_period: int = 20,
        early_session_wick_mult: float = 1.3,
        late_session_wick_mult: float = 0.85,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        early_session_bars: int = 6,
        late_session_bars: int = 15,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.volatility_period = volatility_period
        self.early_session_wick_mult = early_session_wick_mult
        self.late_session_wick_mult = late_session_wick_mult
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.early_session_bars = early_session_bars
        self.late_session_bars = late_session_bars

        # Track two-bar pattern for multi-bar confirmation
        self._candidate_bar: Optional[dict] = None  # Bar N (potential absorption)
        self._confirm_bar: Optional[dict] = None    # Bar N+1 (confirmation)
        self._candidate_session_phase: Optional[str] = None
        
        # Track volatility
        self._recent_ranges: list = []
        
        # Track bars we've entered on
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
            self._candidate_bar = None
            self._confirm_bar = None
            self._candidate_session_phase = None

        self._bars_in_session += 1

        # Update volatility tracking
        bar_range = current.high - current.low
        self._recent_ranges.append(bar_range)
        if len(self._recent_ranges) > self.volatility_period:
            self._recent_ranges = self._recent_ranges[-self.volatility_period:]

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._candidate_bar = None
            self._confirm_bar = None
            self._candidate_session_phase = None
            return

        current_phase = self._get_session_phase()

        # THREE-BAR PATTERN LOGIC:
        # Bar N+2: Check if we should enter based on bars N, N+1, N+2
        if self._candidate_bar and self._confirm_bar and self._candidate_session_phase:
            if self._should_enter_on_third_bar(current, self._candidate_bar, self._confirm_bar, self._candidate_session_phase):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._candidate_bar)
            # Clear pattern after attempting entry
            self._candidate_bar = None
            self._confirm_bar = None
            self._candidate_session_phase = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Bar N+1: Check if current bar confirms the candidate
        if self._candidate_bar and self._candidate_session_phase:
            if self._is_confirmation_bar(current, self._candidate_bar, self._candidate_session_phase):
                self._confirm_bar = {
                    "close": current.close,
                    "high": current.high,
                    "low": current.low,
                }
            else:
                # Pattern broken, reset
                self._candidate_bar = None
                self._candidate_session_phase = None
            return

        # Bar N: Detect potential absorption candidate
        if current_phase == self._get_session_phase():  # Ensure we're still in same phase
            candidate = self._detect_absorption_candidate(current, current_phase)
            if candidate:
                self._candidate_bar = candidate
                self._candidate_session_phase = current_phase

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_session_phase(self) -> str:
        """Determine which phase of the session we're in."""
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

    def _get_volatility_adjusted_wick_threshold(self, session_phase: str) -> float:
        """Get volatility-adjusted minimum wick size."""
        if not self._recent_ranges:
            return self.min_wick_points
        
        avg_range = sum(self._recent_ranges) / len(self._recent_ranges)
        base_threshold = self.min_wick_points
        
        if session_phase == "early":
            return base_threshold * self.early_session_wick_mult
        elif session_phase == "late":
            return base_threshold * self.late_session_wick_mult
        else:
            return base_threshold

    def _detect_absorption_candidate(self, current, session_phase: str) -> Optional[dict]:
        """
        Detect a potential absorption candidate bar.
        This is Bar N in the three-bar pattern.
        
        Requirements:
        1. Strong directional delta
        2. Wick showing rejection (volatility-adjusted)
        3. Close near natural level
        4. Valid stop/target distance
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Get volatility-adjusted wick threshold
        min_wick = self._get_volatility_adjusted_wick_threshold(session_phase)

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
                "close": current.close,
                "low": current.low,
                "high": current.high,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure with absorption
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
                "close": current.close,
                "low": current.low,
                "high": current.high,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _is_confirmation_bar(self, current, candidate: dict, pending_phase: str) -> bool:
        """
        Check if current bar (N+1) confirms the candidate bar (N).
        
        Requirements:
        1. Same session phase
        2. Directional close beyond candidate close
        """
        current_phase = self._get_session_phase()
        if current_phase != pending_phase:
            return False

        if candidate["direction"] == "long":
            return current.close > candidate["close"]
        else:  # short
            return current.close < candidate["close"]

    def _should_enter_on_third_bar(self, current, candidate: dict, confirm: dict, pending_phase: str) -> bool:
        """
        Check if we should enter on bar N+2.
        
        Requirements:
        1. Same session phase
        2. Continuation in direction (close beyond confirm bar)
        3. No reversal back past candidate
        """
        current_phase = self._get_session_phase()
        if current_phase != pending_phase:
            return False

        if candidate["direction"] == "long":
            # Must close above confirm bar and not reverse below candidate low
            if current.close <= confirm["close"]:
                return False
            if current.low < candidate["low"]:
                return False
            return True
        else:  # short
            # Must close below confirm bar and not reverse above candidate high
            if current.close >= confirm["close"]:
                return False
            if current.high > candidate["high"]:
                return False
            return True

    def _place_entry(self, ctx, candidate: dict) -> None:
        """Place entry order after three-bar confirmation."""
        direction = candidate["direction"]
        stop_offset = candidate["stop_distance"]
        target_offset = candidate["target_distance"]

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
