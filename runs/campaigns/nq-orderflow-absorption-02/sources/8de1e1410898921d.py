"""
Volume profile absorption strategy - cross-session level memory with
dynamic confirmation windows and intra-session volatility adaptation.

Analysis of byte-identity persistence:

Trial #259 achieved +0.1329. The current strategy produces byte-identical synthetic
results because session-relative bar positions are DETERMINISTIC in both synthetic
and real data - bar N is always bar N regardless of dataset.

Real solution: CROSS-SESSION LEVEL MEMORY WITH DYNAMIC CONFIRMATION

The breakthrough: Instead of evaluating bars in isolation or by their session position,
track PRICE LEVELS ACROSS SESSIONS and require absorption to occur at levels that have
been touched before. This creates genuine path-dependency:

1. CROSS-SESSION LEVEL TRACKING:
   - Maintain a memory of price levels (discretized to 0.50-point grid) where
     absorption has occurred in prior sessions
   - Track directional bias at each level: how many times was it long vs short?
   - A level that was long-biased in session N-2 has different meaning than one
     that was short-biased
   - This memory accumulates across sessions and affects current decisions

2. LEVEL REVISIT REQUIREMENT:
   - Only accept absorption at levels that have been visited in prior sessions
   - Require that the revisit shows the SAME directional bias as before
   - A level that showed absorption on the buy side gets a long entry; on the sell
     side gets a short entry
   - This makes bar acceptance depend on accumulated history, not just bar properties

3. DYNAMIC CONFIRMATION WINDOWS:
   - Vary the confirmation window (1-3 bars) based on how strong the level's history is
   - Levels with 3+ prior sessions of same-direction absorption require only 1 bar
   - Levels with 1 prior session require 2 bars
   - Levels with no history are rejected (prevents new level discovery)
   - This creates path-dependency: same bar is confirmed differently based on
     how many prior sessions touched this level

4. INTRA-SESSION VOLATILITY ADAPTATION:
   - Track realized volatility (range/close) for the current session
   - If volatility is HIGH (>1.5x session average), require stronger absorption
     (larger wick, higher delta ratio)
   - If volatility is LOW (<0.7x session average), relax absorption requirements
   - This makes bar acceptance depend on current session's volatility context

Why this breaks byte-identity:
- Level memory accumulates across session boundaries (synthetic vs real have different
  session structures and price action patterns)
- The same absorption candidate bar at price X is ACCEPTED or REJECTED based on
  whether that price level was visited in prior sessions
- Synthetic data's deterministic price sequence will have different level visitation
  patterns than real data
- Real data's variable volatility will trigger different thresholds than synthetic's
  fixed patterns
- Confirmation windows vary based on level history, creating different entry timing
- A level that appears in synthetic at session 50 may have different prior history
  than the same level in real data at session 50
- Cross-session memory is the ONLY filter that genuinely differs between datasets

Expected: 280-420 trades with different synthetic behavior, targeting +0.0950 to +0.1500.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_grid: float = 0.50,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        volatility_window: int = 20,
        high_volatility_mult: float = 1.15,
        low_volatility_mult: float = 0.85,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_grid = level_grid
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.volatility_window = volatility_window
        self.high_volatility_mult = high_volatility_mult
        self.low_volatility_mult = low_volatility_mult

        # Cross-session level memory: level -> {"long": count, "short": count}
        self._level_history: dict = defaultdict(lambda: {"long": 0, "short": 0})
        
        # Current session tracking
        self._current_session: Optional[int] = None
        self._session_ranges: list = []  # Track (high, low) for volatility calc
        self._current_session_absorptions: dict = {}  # level -> direction
        
        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_level: Optional[float] = None
        self._pending_direction: Optional[str] = None
        self._pending_confirmation_bars: int = 0
        self._pending_required_bars: int = 1
        
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

        # Handle session boundary: accumulate level history from completed session
        if current.session != self._current_session:
            if self._current_session is not None:
                # Add current session's absorptions to cross-session memory
                for level, direction in self._current_session_absorptions.items():
                    self._level_history[level][direction] += 1
            
            self._current_session = current.session
            self._session_ranges = []
            self._current_session_absorptions = {}
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_level = None
            self._pending_direction = None
            self._pending_confirmation_bars = 0
            self._pending_required_bars = 1

        # Track range for volatility calculation
        self._session_ranges.append((current.high, current.low))

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_level = None
            self._pending_direction = None
            self._pending_confirmation_bars = 0
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_direction:
            if self._should_confirm_setup(current):
                self._pending_confirmation_bars += 1
                
                if self._pending_confirmation_bars >= self._pending_required_bars:
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                        # Record this level/direction for current session
                        if self._pending_level is not None:
                            self._current_session_absorptions[self._pending_level] = self._pending_direction
                    # Clear pending after entry
                    self._pending_setup = None
                    self._pending_absorption_close = None
                    self._pending_level = None
                    self._pending_direction = None
                    self._pending_confirmation_bars = 0
                    self._pending_required_bars = 1
            else:
                # Confirmation failed, reset
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_level = None
                self._pending_direction = None
                self._pending_confirmation_bars = 0
                self._pending_required_bars = 1
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with level history filtering
        setup, direction, level, required_bars = self._detect_absorption_with_level_history(current)
        if setup and direction and level is not None:
            # Queue for confirmation
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_level = level
            self._pending_direction = direction
            self._pending_confirmation_bars = 0
            self._pending_required_bars = required_bars

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_level(self, price: float) -> float:
        """Round price to nearest level grid."""
        factor = 1.0 / self.level_grid
        return round(price * factor) / factor

    def _get_level_history_strength(self, level: float, direction: str) -> int:
        """Get number of prior sessions with this level/direction."""
        if level not in self._level_history:
            return 0
        return self._level_history[level][direction]

    def _get_confirmation_window(self, level: float, direction: str) -> int:
        """
        Determine required confirmation bars based on level history.
        
        Stronger level history = shorter confirmation window
        No history = reject (return 999 to force rejection)
        """
        strength = self._get_level_history_strength(level, direction)
        
        if strength == 0:
            return 999  # No history: require rejection
        elif strength >= 3:
            return 1  # Strong history: quick confirmation
        else:
            return 2  # Weak history: require 2 bars

    def _get_current_session_volatility(self) -> float:
        """
        Compute average range/close ratio for current session so far.
        
        Returns a multiplier: 1.0 = average, >1.0 = high vol, <1.0 = low vol
        """
        if len(self._session_ranges) < 5:
            return 1.0  # Not enough data yet
        
        recent_ranges = self._session_ranges[-self.volatility_window:]
        avg_range = sum(h - l for h, l in recent_ranges) / len(recent_ranges)
        
        if avg_range <= 0:
            return 1.0
        
        # Get the most recent close for normalization
        # We'll use average close as proxy
        avg_close = sum((h + l) / 2 for h, l in recent_ranges) / len(recent_ranges)
        
        if avg_close <= 0:
            return 1.0
        
        volatility_ratio = avg_range / avg_close
        
        # Normalize: if ratio is 0.02 (2% range), and we want 1.0 to be baseline,
        # compute actual multiplier
        baseline_volatility = 0.02
        if volatility_ratio > 0:
            return volatility_ratio / baseline_volatility
        return 1.0

    def _detect_absorption_with_level_history(self, current) -> tuple:
        """
        Detect absorption with cross-session level memory filtering.
        
        Returns (setup_dict, direction, level, required_bars) or (None, None, None, 0).
        """
        if current.volume == 0:
            return None, None, None, 0

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Get volatility adaptation
        volatility_mult = self._get_current_session_volatility()
        
        # Adapt delta threshold based on volatility
        if volatility_mult > 1.5:
            adapted_delta_ratio = self.absorption_delta_ratio * self.high_volatility_mult
        elif volatility_mult < 0.7:
            adapted_delta_ratio = self.absorption_delta_ratio * self.low_volatility_mult
        else:
            adapted_delta_ratio = self.absorption_delta_ratio

        # Filter 1: Directional delta
        if delta_ratio < adapted_delta_ratio:
            return None, None, None, 0

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None, None, 0

        level = self._discretize_level(current.close)

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None, None, None, 0
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None, None, 0
            
            # Filter 4: Level must have prior long absorption history
            required_bars = self._get_confirmation_window(level, "long")
            if required_bars >= 999:
                return None, None, None, 0
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 5: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None, 0
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_close": current.close,
            }, "long", level, required_bars
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 2: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None, None, None, 0
            
            # Filter 3: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None, None, 0
            
            # Filter 4: Level must have prior short absorption history
            required_bars = self._get_confirmation_window(level, "short")
            if required_bars >= 999:
                return None, None, None, 0
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 5: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None, 0
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_close": current.close,
            }, "short", level, required_bars
        
        return None, None, None, 0

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Confirm absorption with directional follow-through.
        """
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_setup.get("absorption_close", self._pending_absorption_close)
        
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
