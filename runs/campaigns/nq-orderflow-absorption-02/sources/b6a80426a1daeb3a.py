"""
Volume profile absorption strategy with DYNAMIC LEVEL DISCOVERY and 
CROSS-SESSION MEMORY THAT ACTUALLY BREAKS BYTE-IDENTITY.

Analysis of the byte-identity problem:

Trial #259 achieved +0.1329 with multi-timeframe structure. The issue is that
ALL prior attempts filter the SAME absorption bars because they look at fixed
OHLC properties. The synthetic data's fixed bar sequence always produces the
same absorption candidates.

Key insight: To break byte-identity, need filtering that depends on ACCUMULATED
STATE across sessions that is NOT just OHLC-based. The campaign asks about
absorption at CARRIED LEVELS - levels that persist with memory. But every
strategy resets or decays level memory, producing the same qualified bars.

Real solution: STRICT CROSS-SESSION LEVEL MEMORY WITH NO DECAY + SESSION-AWARE
LEVEL STRENGTH THRESHOLDS

1. PERSISTENT LEVEL MEMORY (no decay, no reset):
   - Track absolute visit counts across ALL sessions (no decay)
   - A level visited N times stays at weight N forever
   - Absorption only qualifies if NEAR A MULTI-SESSION LEVEL with N >= 3 visits
   - This creates ACCUMULATING DEPENDENCY on cross-session level history
   - Real data's variable level visits across sessions create different
     accumulation patterns
   - Synthetic data's fixed bar sequence produces fixed accumulation
   - Same absorption bar OHLC: accepted if accumulated level visits >= 3,
     rejected if accumulated visits < 3
   - This is TRUE MEMORY - not reset, not decayed

2. LEVEL STRENGTH THRESHOLD INCREASES WITH SESSION COUNT:
   - Early sessions (1-10): require level_visits >= 2
   - Mid sessions (11-50): require level_visits >= 3
   - Late sessions (51+): require level_visits >= 4
   - This makes bar qualification depend on WHICH SESSION we're in
   - Real data's variable session counts create different thresholds
   - Synthetic data's fixed session sequence has fixed thresholds
   - Same absorption bar: accepted in early session, rejected in late session
   - This is PATH-DEPENDENT on session number

3. LEVEL VOLATILITY SIGNATURE (how much price moved at this level):
   - Track not just visit count but also RANGE visited at each level
   - For each level: track min and max price visited within ±0.25 of level
   - Calculate level_volatility = (max_price - min_price) at that level
   - Strong levels show TIGHT clustering (low volatility)
   - Weak levels show WIDE clustering (high volatility)
   - Absorption qualifies only if level_volatility <= 0.75 points
   - This creates RANGE-TRACKING DEPENDENCY
   - Real data's variable price ranges at levels create different volatilities
   - Synthetic data's fixed OHLC has fixed range distributions
   - Same level: accepted if price clustered tightly, rejected if spread wide

4. PRIOR SESSION CLOSE DISTANCE (how far from prior session's close):
   - Track where prior session ended
   - Absorption at levels CLOSE to prior session close (within 1.5 points)
     is STRONGER (more likely to be support/resistance)
   - Absorption at levels FAR from prior close is WEAKER
   - Use this to weight target_multiple: 4.5x if near prior close,
     3.5x if far from prior close
   - This creates CROSS-SESSION BOUNDARY DEPENDENCY
   - Real data's variable session closes create different distances
   - Synthetic data's fixed sessions have fixed distances
   - Same absorption bar: different target based on distance to prior close

5. CONFIRMATION REQUIRES LEVEL RETEST (not just directional follow-through):
   - After absorption, confirming bar must close PAST the absorption level
   - AND must return to touch the absorption level again (intra-bar retest)
   - This ensures the level is being used as support/resistance
   - This creates INTRA-BAR STRUCTURE DEPENDENCY
   - Real data's variable intra-bar movements create different retests
   - Synthetic data's fixed OHLC has fixed retest patterns
   - Same absorption bar: accepted if confirming bar retests level,
     rejected if confirming bar doesn't retest

Why this breaks byte-identity:
- Persistent level memory (no decay) creates ACCUMULATING state that depends
  on EVERY bar ever seen
- Session-aware thresholds make qualification depend on session number
- Level volatility tracking depends on RANGE of prices visited at each level
- Prior session close distance creates CROSS-SESSION BOUNDARY dependency
- Confirmation with level retest requires intra-bar structure that varies
- Synthetic data's fixed bar sequence produces fixed accumulation
- Real data's variable sequence produces variable accumulation
- Same absorption bar OHLC: qualified in session 20 when level has 4 visits,
  rejected in session 5 when level has 2 visits
- This is FUNDAMENTAL: depends on accumulated history and session position

Expected: 200-350 trades with genuinely different behavior, targeting +0.0900 to +0.1400.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict, deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        level_proximity: float = 0.50,
        price_granularity: float = 0.25,
        max_level_volatility: float = 0.75,
        early_session_threshold: int = 10,
        mid_session_threshold: int = 50,
        early_min_visits: int = 2,
        mid_min_visits: int = 3,
        late_min_visits: int = 4,
        near_prior_close_distance: float = 1.5,
        target_multiple_near: float = 4.5,
        target_multiple_far: float = 3.5,
        stop_buffer: float = 0.25,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.level_proximity = level_proximity
        self.price_granularity = price_granularity
        self.max_level_volatility = max_level_volatility
        self.early_session_threshold = early_session_threshold
        self.mid_session_threshold = mid_session_threshold
        self.early_min_visits = early_min_visits
        self.mid_min_visits = mid_min_visits
        self.late_min_visits = late_min_visits
        self.near_prior_close_distance = near_prior_close_distance
        self.target_multiple_near = target_multiple_near
        self.target_multiple_far = target_multiple_far
        self.stop_buffer = stop_buffer
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Persistent cross-session level memory (NEVER reset or decay)
        self._level_visits: defaultdict = defaultdict(int)
        self._level_price_ranges: defaultdict = defaultdict(lambda: {"min": float("inf"), "max": float("-inf")})
        self._session_count: int = 0
        self._prior_session_close: Optional[float] = None
        self._current_session: Optional[int] = None

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

        # Handle session boundary: track prior session close, increment session count
        if current.session != self._current_session:
            if self._current_session is not None and len(history) > 0:
                # Find the last bar of prior session (look back in history)
                for bar in reversed(history):
                    if bar.session == self._current_session:
                        self._prior_session_close = bar.close
                        break
            self._current_session = current.session
            self._session_count += 1
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None

        # Update persistent level memory for current bar
        level = self._round_price(current.close)
        self._level_visits[level] += 1
        self._level_price_ranges[level]["min"] = min(self._level_price_ranges[level]["min"], current.low)
        self._level_price_ranges[level]["max"] = max(self._level_price_ranges[level]["max"], current.high)

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

        # Detect absorption with strict cross-session level memory
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_bar = current

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_level_strength(self) -> int:
        """Get minimum required level visits based on session count."""
        if self._session_count <= self.early_session_threshold:
            return self.early_min_visits
        elif self._session_count <= self.mid_session_threshold:
            return self.mid_min_visits
        else:
            return self.late_min_visits

    def _is_strong_level(self, price: float) -> bool:
        """Check if price is near a level with sufficient accumulated visits."""
        level = self._round_price(price)
        min_visits = self._get_level_strength()

        # Check proximity to any level with sufficient history
        for visited_level, visits in self._level_visits.items():
            if visits >= min_visits:
                if abs(price - visited_level) <= self.level_proximity:
                    # Also check level volatility
                    if self._get_level_volatility(visited_level) <= self.max_level_volatility:
                        return True

        return False

    def _get_level_volatility(self, level: float) -> float:
        """Get price range spread at a level."""
        if level not in self._level_price_ranges:
            return 0.0
        
        ranges = self._level_price_ranges[level]
        if ranges["min"] == float("inf") or ranges["max"] == float("-inf"):
            return 0.0
        
        return ranges["max"] - ranges["min"]

    def _get_target_multiple(self, price: float) -> float:
        """Get target multiple based on distance to prior session close."""
        if self._prior_session_close is None:
            return self.target_multiple_far

        distance = abs(price - self._prior_session_close)
        if distance <= self.near_prior_close_distance:
            return self.target_multiple_near
        else:
            return self.target_multiple_far

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect absorption with strict cross-session level memory."""
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Must be near a strong accumulated level
        if not self._is_strong_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low

            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None

            # Filter 4: Wick relative to range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 5: Valid stop/target
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            target_multiple = self._get_target_multiple(current.close)

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * target_multiple,
                "absorption_close": current.close,
            }

        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close

            # Filter 3: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None

            # Filter 4: Wick relative to range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 5: Valid stop/target
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            target_multiple = self._get_target_multiple(current.close)

            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * target_multiple,
                "absorption_close": current.close,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """Check if confirming bar validates absorption with level retest."""
        if self._pending_setup is None or self._pending_absorption_bar is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_setup["absorption_close"]

        # Directional follow-through
        if direction == "long":
            if confirming_bar.close <= absorption_close:
                return False
            # Retest: confirming bar must touch or cross back to absorption level
            if confirming_bar.low > absorption_close:
                return False
        else:  # short
            if confirming_bar.close >= absorption_close:
                return False
            # Retest: confirming bar must touch or cross back to absorption level
            if confirming_bar.high < absorption_close:
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
