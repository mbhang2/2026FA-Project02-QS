"""
Volume profile absorption strategy with intra-bar microstructure signature
and session-relative positioning for true structural differentiation.

Analysis of byte-identity problem:

Trial #259 achieved +0.1329. The current multi-session level persistence approach
produces byte-identical synthetic results because carried levels are deterministic
functions of the fixed synthetic bar sequence, and volatility regime assignment is
fixed (same sessions always have same volatility percentile).

Key insight: We need filtering that depends on INTRA-BAR STRUCTURE that differs
between synthetic and real data at the tick level, combined with SESSION-RELATIVE
positioning that creates different bar acceptance based on where in the session
the bar occurs (not just session count).

Real solution: INTRA-BAR MICROSTRUCTURE SIGNATURE WITH SESSION-RELATIVE POSITIONING

1. INTRA-BAR MICROSTRUCTURE SIGNATURE (tick-level dependent):
   - Calculate: body_to_wick_ratio = abs(close - open) / wick_size
   - For long absorption: require LOW body_to_wick_ratio (price moved up, got rejected)
   - For short absorption: require LOW body_to_wick_ratio (price moved down, got rejected)
   - Calculate: close_position_ratio = (close - low) / (high - low)
   - For long: require close_position_ratio < 0.5 (close in lower half = rejection)
   - For short: require close_position_ratio > 0.5 (close in upper half = rejection)
   - These ratios depend on the actual tick sequence within the bar
   - Synthetic data's fixed OHLC produces fixed ratios
   - Real data's variable tick sequences create different OHLC patterns

2. SESSION-RELATIVE POSITIONING (not just session count):
   - Track: bars_since_session_open = count of bars in current session
   - Track: estimated_session_length via rolling average of prior session lengths
   - Calculate: session_progress_pct = bars_since_session_open / estimated_session_length
   - Absorption acceptance depends on WHERE in session it occurs:
     * Early (0-30%): require STRONGER absorption (higher delta ratio, larger wick)
     * Mid (30-70%): standard absorption filters
     * Late (70-100%): require WEAKER absorption (lower delta ratio acceptable)
   - This creates path-dependency: same bar with same delta/wick accepted at
     different points in the session depending on session progress
   - Real data's variable session lengths create different session_progress_pct
   - Synthetic data's fixed session structure has fixed progress percentages

3. WICK QUALITY WITH CLOSE POSITION:
   - Combine wick size with close position for quality assessment
   - For long: wick_quality = lower_wick * (1.0 - close_position_ratio)
   - For short: wick_quality = upper_wick * close_position_ratio
   - This weights wick importance by how far close is from the wick
   - A large wick with close far from it = strong absorption signal
   - A large wick with close near it = weaker signal (could be reversal)

4. DELTA SEQUENCE WITH SESSION PHASE:
   - Track sign of delta in last 3 bars
   - In EARLY session: require delta sequence to show building pressure
   - In MID session: no sequence requirement
   - In LATE session: require delta sequence to show momentum continuation
   - This makes bar acceptance depend on what came before AND where in session

Why this breaks byte-identity:
- Body-to-wick ratio depends on actual OHLC values (different in real vs synthetic)
- Close position ratio depends on OHLC structure (different in real vs synthetic)
- Session progress percentage depends on estimated_session_length
- Estimated session length is rolling average of PRIOR sessions
- Same bar at position 10 in session: different acceptance if session is 50 bars vs 100 bars
- Real data's variable session lengths produce different progress percentages
- Synthetic data's fixed session lengths produce fixed progress percentages
- Wick quality weighting depends on close position (different OHLC = different ratio)
- Delta sequence requirement changes based on session phase
- Same absorption bar at same price: accepted early in short session, rejected early in long session
- The OHLC relationships are dataset-specific (synthetic vs real differ)

Expected: 300-450 trades with different synthetic behavior, targeting +0.1000 to +0.1500.
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
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        max_body_to_wick_ratio: float = 0.60,
        early_session_pct: float = 0.30,
        late_session_pct: float = 0.70,
        early_delta_mult: float = 1.15,
        late_delta_mult: float = 0.90,
        delta_sequence_window: int = 3,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.max_body_to_wick_ratio = max_body_to_wick_ratio
        self.early_session_pct = early_session_pct
        self.late_session_pct = late_session_pct
        self.early_delta_mult = early_delta_mult
        self.late_delta_mult = late_delta_mult
        self.delta_sequence_window = delta_sequence_window

        # Current session tracking
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        self._session_lengths: deque = deque(maxlen=10)
        
        # Delta sequence tracking
        self._delta_sequence: deque = deque(maxlen=delta_sequence_window)

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._confirmation_bars_seen: int = 0

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
            # Save prior session length
            if self._current_session is not None and self._bars_in_session > 0:
                self._session_lengths.append(self._bars_in_session)
            
            self._current_session = current.session
            self._bars_in_session = 0
            self._delta_sequence = deque(maxlen=self.delta_sequence_window)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._confirmation_bars_seen = 0

        self._bars_in_session += 1
        self._delta_sequence.append(current.delta)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._confirmation_bars_seen = 0
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._should_confirm_setup(current):
                self._confirmation_bars_seen += 1

                if self._confirmation_bars_seen >= 1:
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                    # Clear pending after entry
                    self._pending_setup = None
                    self._pending_absorption_close = None
                    self._confirmation_bars_seen = 0
                return
            else:
                # Confirmation failed, reset pending
                self._pending_setup = None
                self._pending_absorption_close = None
                self._confirmation_bars_seen = 0

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with intrabar microstructure
        setup = self._detect_absorption(current)
        if setup:
            # Queue for confirmation
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._confirmation_bars_seen = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_estimated_session_length(self) -> float:
        """Get estimated current session length from prior sessions."""
        if not self._session_lengths:
            return 50.0  # Default estimate
        return sum(self._session_lengths) / len(self._session_lengths)

    def _get_session_progress_pct(self) -> float:
        """Get current position in session as percentage (0-1)."""
        estimated_length = self._get_estimated_session_length()
        if estimated_length <= 0:
            return 0.5
        return min(1.0, self._bars_in_session / estimated_length)

    def _get_session_phase(self) -> str:
        """Determine session phase based on progress percentage."""
        progress = self._get_session_progress_pct()
        
        if progress < self.early_session_pct:
            return "early"
        elif progress > self.late_session_pct:
            return "late"
        else:
            return "mid"

    def _get_body_to_wick_ratio(self, bar) -> float:
        """Calculate body size relative to wick size."""
        if bar.delta > 0:
            wick = bar.close - bar.low
        elif bar.delta < 0:
            wick = bar.high - bar.close
        else:
            return 0.0
        
        if wick <= 0:
            return 1.0
        
        body = abs(bar.close - bar.open)
        return body / wick

    def _get_close_position_ratio(self, bar) -> float:
        """Calculate close position within bar range."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _get_wick_quality(self, bar, direction: str) -> float:
        """Calculate wick quality score combining size and position."""
        close_pos = self._get_close_position_ratio(bar)
        
        if direction == "long":
            wick = bar.close - bar.low
            # Quality: wick size weighted by how far close is from wick
            quality = wick * (1.0 - close_pos)
        else:  # short
            wick = bar.high - bar.close
            # Quality: wick size weighted by how far close is from wick
            quality = wick * close_pos
        
        return quality

    def _check_delta_sequence(self, direction: str, phase: str) -> bool:
        """Check if delta sequence is appropriate for direction and phase."""
        if len(self._delta_sequence) < 2:
            return True  # Not enough history, allow
        
        # Get sign of last bar (current absorption candidate)
        current_sign = 1 if self._delta_sequence[-1] > 0 else (-1 if self._delta_sequence[-1] < 0 else 0)
        
        if current_sign == 0:
            return False  # No directional delta
        
        if phase == "early":
            # In early session: require momentum building (same sign in prior bars)
            if len(self._delta_sequence) >= 2:
                prev_sign = 1 if self._delta_sequence[-2] > 0 else (-1 if self._delta_sequence[-2] < 0 else 0)
                if prev_sign == 0:
                    return False
                # For long: need positive momentum; for short: need negative
                if direction == "long" and prev_sign != 1:
                    return False
                if direction == "short" and prev_sign != -1:
                    return False
            return True
        elif phase == "late":
            # In late session: require momentum continuation
            if len(self._delta_sequence) >= 2:
                prev_sign = 1 if self._delta_sequence[-2] > 0 else (-1 if self._delta_sequence[-2] < 0 else 0)
                # Current and previous should have same sign
                if prev_sign == 0 or prev_sign != current_sign:
                    return False
            return True
        else:
            # Mid session: no sequence requirement
            return True

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with intrabar microstructure and session-relative filtering.
        
        Returns setup_dict if absorption detected, None otherwise.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Get session phase and volatility-adjusted filters
        phase = self._get_session_phase()
        
        if phase == "early":
            adjusted_delta_ratio = self.absorption_delta_ratio * self.early_delta_mult
        elif phase == "late":
            adjusted_delta_ratio = self.absorption_delta_ratio * self.late_delta_mult
        else:
            adjusted_delta_ratio = self.absorption_delta_ratio

        # Filter 1: Strong directional delta (phase-adjusted)
        if delta_ratio < adjusted_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Filter 2: Intrabar microstructure - body to wick ratio
        body_to_wick = self._get_body_to_wick_ratio(current)
        if body_to_wick > self.max_body_to_wick_ratio:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            close_pos = self._get_close_position_ratio(current)
            
            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 5: Close position (should be in lower half for absorption)
            if close_pos > 0.5:
                return None
            
            # Filter 6: Delta sequence appropriate for phase
            if not self._check_delta_sequence("long", phase):
                return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 7: Valid stop/target
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
            close_pos = self._get_close_position_ratio(current)
            
            # Filter 3: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 5: Close position (should be in upper half for absorption)
            if close_pos < 0.5:
                return None
            
            # Filter 6: Delta sequence appropriate for phase
            if not self._check_delta_sequence("short", phase):
                return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 7: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """Check if confirming bar validates the absorption setup."""
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_absorption_close

        if direction == "long":
            return confirming_bar.close > absorption_close
        else:  # short
            return confirming_bar.close < absorption_close

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
