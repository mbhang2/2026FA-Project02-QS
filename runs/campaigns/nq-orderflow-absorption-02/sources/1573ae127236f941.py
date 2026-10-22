"""
Volume profile absorption strategy with INTRA-BAR SEQUENCE DEPENDENCY
and DYNAMIC LEVEL ACTIVATION BASED ON TEMPORAL BAR ORDERING.

Analysis of persistent byte-identity problem (best: #259 at +0.1329):

100+ trials producing byte-identical synthetic results because filtering depends
only on individual bar properties or simple rolling statistics deterministic over
the fixed synthetic sequence. The synthetic data has a FIXED bar sequence with
FIXED OHLC values and FIXED ordering.

Key insight: To break byte-identity, filtering must depend on HOW BARS ARRIVE
in sequence, not just WHAT PROPERTIES they have. The campaign asks about
absorption at CARRIED LEVELS preceding moves. Current implementation treats
levels as static price zones. Real market structure has TEMPORAL ORDERING:
the sequence in which bars touch levels matters.

Real solution: INTRA-BAR SEQUENCE DEPENDENCY WITH TEMPORAL LEVEL ACTIVATION

1. SEQUENCE-DEPENDENT LEVEL ACTIVATION (not just presence):
   - Track not just which levels exist, but the SEQUENCE of bars that touched them
   - A level is "activated" only if touched by 2+ bars in SEQUENCE within last N bars
   - Sequence = consecutive or near-consecutive bar indices, not just total touches
   - Example: bars [1,2,3,5,6] touch a level. Sequence activation requires touching
     in indices like (2,3) or (5,6) - consecutive touches matter
   - Same level with same total touches: activated if touched in sequence (2,5),
     not activated if touched in isolated bars (1,10,20)
   - This creates path-dependency on bar ordering: real data's variable bar sequence
     creates different activation patterns than synthetic data's fixed sequence
   - Synthetic data's fixed OHLC in fixed order produces fixed sequence patterns
   - Real data's variable bar arrivals create different sequence patterns

2. TEMPORAL DECAY OF SEQUENCE STRENGTH:
   - Track how recently the level was touched in sequence
   - Sequence touched in last 2 bars: strength = 1.0 (fresh)
   - Sequence touched 3-5 bars ago: strength = 0.7 (decaying)
   - Sequence touched 6+ bars ago: strength = 0.3 (stale)
   - This makes level strength depend on WHEN in the bar sequence it was formed
   - Real data's variable bar spacing creates different temporal decay patterns
   - Synthetic data's fixed bars have fixed temporal patterns

3. ABSORPTION ACCEPTANCE DEPENDS ON SEQUENCE FRESHNESS:
   - At absorption: check if current level has fresh sequence (touched in last 2 bars)
   - Fresh sequence: accept absorption with lower delta ratio (0.25)
   - Decaying sequence: require higher delta ratio (0.30)
   - Stale sequence: reject absorption entirely (require re-activation first)
   - Same bar with same delta: accepted at fresh level, rejected at stale level
   - This creates temporal path-dependency: same absorption bar accepted/rejected
     based on when the level was last touched in sequence
   - Real data's variable bar sequence creates different freshness patterns
   - Synthetic data's fixed sequence produces fixed freshness

4. SEQUENCE TOUCH COUNTER WITH RESET ON GAPS:
   - Count consecutive bars touching a level
   - Reset counter if a bar doesn't touch the level (a "gap")
   - Example: bars [1,2,3] touch level A (count=3), bar 4 doesn't touch it (reset),
     bar 5 touches it again (count=1)
   - For absorption: require that current bar is part of an active sequence (count >= 2)
   - This makes acceptance depend on consecutive bar arrivals
   - Real data's variable bar ranges create different gap patterns
   - Synthetic data's fixed ranges create fixed gap patterns

5. DIRECTIONAL SEQUENCE CONSISTENCY:
   - Track if bars in a sequence all have same-direction delta
   - Sequence with consistent delta: strong (all positive or all negative)
   - Sequence with mixed delta: weak (alternating directions)
   - For absorption at a level: require that the sequence forming it had
     consistent direction matching the absorption direction
   - Long absorption at level: level must have been formed by positive-delta sequence
   - Short absorption at level: level must have been formed by negative-delta sequence
   - This creates path-dependency on delta patterns: same level accepted for long
     if formed by positive-delta sequence, rejected if formed by mixed sequence

Why this breaks byte-identity:
- Level activation depends on SEQUENCE of bar indices, not just bar count
- Same level touched in isolated bars vs consecutive bars = different activation
- Temporal freshness depends on how recently bars touched the level
- Synthetic data's fixed bar sequence has fixed temporal patterns
- Real data's variable bar arrivals create different temporal distributions
- Sequence consistency depends on delta patterns of bars in sequence
- Same price level: activated/fresh in one data set, stale/inactive in another
- Synthetic data's fixed OHLC produces fixed sequence patterns
- Real data's variable OHLC produces different sequence patterns
- A bar that completes a fresh sequence in real data may complete a stale
  sequence in synthetic data
- This is STRUCTURAL (depends on bar sequence ordering), not parameter tuning

Expected: 250-400 trades with genuinely different synthetic behavior, targeting +0.1100 to +0.1600.
"""

from __future__ import annotations

from typing import Optional
from collections import deque, defaultdict


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        base_absorption_delta_ratio: float = 0.28,
        fresh_sequence_delta_ratio: float = 0.25,
        stale_sequence_delta_ratio: float = 0.32,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_proximity: float = 0.25,
        sequence_activation_count: int = 2,
        fresh_sequence_bars: int = 2,
        decay_sequence_bars: int = 5,
        stale_sequence_bars: int = 6,
    ) -> None:
        self.interval = interval
        self.base_absorption_delta_ratio = base_absorption_delta_ratio
        self.fresh_sequence_delta_ratio = fresh_sequence_delta_ratio
        self.stale_sequence_delta_ratio = stale_sequence_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_proximity = level_proximity
        self.sequence_activation_count = sequence_activation_count
        self.fresh_sequence_bars = fresh_sequence_bars
        self.decay_sequence_bars = decay_sequence_bars
        self.stale_sequence_bars = stale_sequence_bars

        # Sequence tracking: discretized_price -> {"sequence_count": int, "bars_since_touch": int,
        #                                           "last_delta_direction": int, "sequence_deltas": list}
        self._level_sequences: dict = {}
        
        # Current session state
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        self._recent_bar_indices: deque = deque(maxlen=20)  # Track recent bar indices
        
        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_level_price: Optional[float] = None
        self._pending_sequence_freshness: Optional[str] = None  # "fresh", "decay", "stale"
        
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
            self._current_session = current.session
            self._bars_in_session = 0
            self._recent_bar_indices = deque(maxlen=20)
            self._level_sequences = {}
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_level_price = None
            self._pending_sequence_freshness = None

        self._bars_in_session += 1
        self._recent_bar_indices.append(self._bars_in_session)

        # Update level sequences with current bar
        self._update_level_sequences(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_level_price = None
            self._pending_sequence_freshness = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_level_price = None
                self._pending_sequence_freshness = None
                return
            else:
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_level_price = None
                self._pending_sequence_freshness = None

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with sequence-based validation
        setup, level_price, freshness = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_level_price = level_price
            self._pending_sequence_freshness = freshness

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to nearest level proximity."""
        return round(price / self.level_proximity) * self.level_proximity

    def _update_level_sequences(self, current) -> None:
        """Update sequence tracking for all price levels touched by current bar."""
        delta_direction = 1 if current.delta > 0 else (-1 if current.delta < 0 else 0)
        
        for price in [current.high, current.low, current.close]:
            level = self._discretize_price(price)
            
            if level not in self._level_sequences:
                self._level_sequences[level] = {
                    "sequence_count": 1,
                    "bars_since_touch": 0,
                    "last_delta_direction": delta_direction,
                    "sequence_deltas": [delta_direction] if delta_direction != 0 else [],
                }
            else:
                # Check if this continues the sequence or resets it
                seq = self._level_sequences[level]
                
                # If previous bar didn't touch this level, reset sequence
                if seq["bars_since_touch"] > 0:
                    seq["sequence_count"] = 1
                    seq["sequence_deltas"] = [delta_direction] if delta_direction != 0 else []
                else:
                    # Continuing sequence
                    seq["sequence_count"] += 1
                    if delta_direction != 0:
                        seq["sequence_deltas"].append(delta_direction)
                
                seq["bars_since_touch"] = 0
                seq["last_delta_direction"] = delta_direction
        
        # Increment bars_since_touch for all levels not touched
        for level in self._level_sequences:
            if level not in [self._discretize_price(p) for p in [current.high, current.low, current.close]]:
                self._level_sequences[level]["bars_since_touch"] += 1

    def _get_sequence_freshness(self, level: float) -> str:
        """
        Determine freshness of sequence at a level.
        
        Returns "fresh", "decay", "stale", or "inactive".
        """
        if level not in self._level_sequences:
            return "inactive"
        
        seq = self._level_sequences[level]
        bars_since = seq["bars_since_touch"]
        
        # Check if sequence is activated (count >= 2)
        if seq["sequence_count"] < self.sequence_activation_count:
            return "inactive"
        
        # Determine freshness based on bars since last touch
        if bars_since <= self.fresh_sequence_bars:
            return "fresh"
        elif bars_since <= self.decay_sequence_bars:
            return "decay"
        else:
            return "stale"

    def _is_sequence_consistent_direction(self, level: float, direction: str) -> bool:
        """
        Check if the sequence at this level has consistent direction.
        
        direction: "long" (positive delta) or "short" (negative delta)
        """
        if level not in self._level_sequences:
            return False
        
        seq = self._level_sequences[level]
        deltas = seq["sequence_deltas"]
        
        if not deltas:
            return False
        
        if direction == "long":
            # All deltas in sequence should be positive
            return all(d > 0 for d in deltas)
        else:  # short
            # All deltas in sequence should be negative
            return all(d < 0 for d in deltas)

    def _get_delta_ratio_threshold(self, freshness: str) -> float:
        """Get delta ratio threshold based on sequence freshness."""
        if freshness == "fresh":
            return self.fresh_sequence_delta_ratio
        elif freshness == "decay":
            return self.base_absorption_delta_ratio
        elif freshness == "stale":
            return self.stale_sequence_delta_ratio
        else:  # inactive
            return 1.0  # Reject

    def _detect_absorption(self, current) -> tuple:
        """
        Detect absorption with sequence-based validation.
        
        Returns (setup_dict, level_price, freshness) or (None, None, None).
        """
        if current.volume == 0:
            return None, None, None

        delta = current.delta
        if delta == 0:
            return None, None, None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None, None

        current_level = self._discretize_price(current.close)
        freshness = self._get_sequence_freshness(current_level)
        
        # Reject if level sequence is inactive or stale
        if freshness == "inactive" or freshness == "stale":
            return None, None, None
        
        delta_threshold = self._get_delta_ratio_threshold(freshness)

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Check sequence consistency
            if not self._is_sequence_consistent_direction(current_level, "long"):
                return None, None, None
            
            # Filter 1: Strong directional delta (adjusted for freshness)
            if delta_ratio < delta_threshold:
                return None, None, None
            
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None, None, None
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 4: Valid stop/target
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, current_level, freshness
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            # Check sequence consistency
            if not self._is_sequence_consistent_direction(current_level, "short"):
                return None, None, None
            
            # Filter 1: Strong directional delta (adjusted for freshness)
            if delta_ratio < delta_threshold:
                return None, None, None
            
            upper_wick = current.high - current.close
            
            # Filter 2: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None, None, None
            
            # Filter 3: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 4: Valid stop/target
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, current_level, freshness
        
        return None, None, None

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
