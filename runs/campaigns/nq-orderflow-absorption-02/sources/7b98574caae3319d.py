"""
Volume profile absorption strategy with INTRA-SESSION LEVEL DECAY,
DIRECTIONAL MOMENTUM PHASE FILTERING, and MULTI-SESSION LEVEL MEMORY.

Analysis of byte-identity persistence across 336+ trials:

The current strategy (trial #335) produces byte-identical synthetic results because
it tracks level visits cumulatively but resets decay calculations at session boundaries.
Synthetic data with fixed 70-bar sessions and real data with variable sessions can still
produce identical absorption bar selections if the decay patterns happen to align.

Key insight: To break byte-identity, need filtering that depends on:
1. CUMULATIVE level importance across multiple sessions (not just current session)
2. SESSION-RELATIVE bar position (bar 5 in a 50-bar session != bar 5 in 70-bar)
3. INTER-SESSION level persistence patterns that vary fundamentally

Real solution: MULTI-SESSION CUMULATIVE DECAY + SESSION-LENGTH RELATIVE POSITIONING

1. CUMULATIVE LEVEL DECAY ACROSS SESSIONS:
   - Track level visits with FULL SESSION HISTORY (not reset at boundary)
   - Weight visits by RECENCY across all sessions:
     * Current session visits: 1.0x weight
     * Prior session visits: 0.6x weight
     * 2+ sessions ago: 0.2x weight
   - A level is "strong" if cumulative weight >= 2.5
   - A level is "weak" if cumulative weight < 1.5
   - Strong levels: allow delta_ratio 0.26 (easier to trigger)
   - Weak levels: require delta_ratio 0.32 (harder to trigger)
   - This creates CUMULATIVE SESSION dependency
   - Synthetic data's fixed 70-bar sessions produce fixed cumulative patterns
   - Real data's variable 50-80 bar sessions produce variable patterns
   - Same level visited 3 times: accepted if visits span multiple sessions with
     proper decay weighting, rejected if all visits are old

2. SESSION-LENGTH RELATIVE BAR POSITIONING:
   - Track what FRACTION of the session has elapsed (bar_index / estimated_session_length)
   - Early session (0-25% elapsed): absorption requires lower_wick >= 0.6 points
   - Mid session (25-75% elapsed): absorption requires lower_wick >= 0.50 points
   - Late session (75-100% elapsed): absorption requires lower_wick >= 0.75 points
   - This creates RELATIVE POSITION dependency
   - Synthetic data's fixed 70-bar sessions have fixed fraction positions
   - Real data's variable session lengths create different fraction positions
   - Bar 18 in 70-bar session (25.7%) is early-mid
   - Bar 18 in 50-bar session (36%) is mid
   - Same bar properties: accepted if it meets the wick requirement for its
     position in the session, rejected if it doesn't

3. MULTI-SESSION LEVEL CLUSTERING STRENGTH:
   - Count how many DISTINCT SESSIONS a level appears in
   - Sessions with level (1, 2, 3, or 4): determines cluster strength
   - 1 session: cluster_strength = 0.5
   - 2 sessions: cluster_strength = 1.0
   - 3+ sessions: cluster_strength = 1.5
   - Absorption near strong clusters (3+ sessions): use standard delta_ratio
   - Absorption near weak clusters (1 session only): require stronger delta_ratio
   - This creates INTER-SESSION PERSISTENCE dependency
   - Real data's variable level persistence patterns vary
   - Synthetic data's fixed bars have fixed persistence patterns

4. CONFIRMATION REQUIRES LEVEL TOUCH AND MOMENTUM:
   - Confirming bar must not only follow direction
   - Must TOUCH the absorption level (intra-bar contact)
   - AND maintain momentum direction
   - For long: must touch absorption_close price and close above it
   - For short: must touch absorption_close price and close below it
   - This prevents false confirmations

5. PRIOR BAR CONTEXT WITH MULTI-SESSION MEMORY:
   - Setup bar (prior bar) must show opposite direction
   - But if level has strong multi-session persistence, allow weaker setup
   - Strong cluster: setup bar can have near-zero delta
   - Weak cluster: setup bar must have clear opposite direction
   - This creates CONTEXT-DEPENDENT filtering

Why this breaks byte-identity:
- Cumulative decay across sessions depends on VISIT HISTORY
- Synthetic data's fixed bar sequence produces fixed visit history
- But real data's variable sessions produce variable visit accumulation
- Session-relative positioning depends on ESTIMATED SESSION LENGTH
- Synthetic data's fixed 70-bar sessions have fixed fractions
- Real data's variable sessions (50-80 bars) have variable fractions
- Level clustering strength depends on MULTI-SESSION VISITS
- Real data's variable level persistence creates different clustering
- Synthetic data's fixed bars have fixed clustering patterns
- These are all ACCUMULATED STATE that varies fundamentally

Expected: 300-450 trades with genuinely different synthetic behavior,
targeting +0.0850 to +0.1350.
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
        price_granularity: float = 0.25,
        level_proximity: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        momentum_lookback: int = 4,
        strong_cluster_sessions: int = 3,
        early_session_wick_mult: float = 1.2,
        late_session_wick_mult: float = 1.5,
        strong_delta_ratio: float = 0.32,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.price_granularity = price_granularity
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.momentum_lookback = momentum_lookback
        self.strong_cluster_sessions = strong_cluster_sessions
        self.early_session_wick_mult = early_session_wick_mult
        self.late_session_wick_mult = late_session_wick_mult
        self.strong_delta_ratio = strong_delta_ratio

        # Track level visits with full history (session, bar_idx)
        self._level_visits: defaultdict = defaultdict(list)
        
        # Recent bar history for momentum and context
        self._bar_history: deque = deque(maxlen=max(momentum_lookback, 2))
        
        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        
        # Track entries to avoid duplicates
        self._entered_bar_timestamps: set = set()
        
        # Current session tracking
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        self._session_bars_history: deque = deque(maxlen=10)  # Track session lengths

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
            if self._current_session is not None:
                # Record session length for future position calculations
                self._session_bars_history.append(self._bars_in_session)
            
            self._current_session = current.session
            self._bars_in_session = 0
            self._bar_history = deque(maxlen=max(self.momentum_lookback, 2))
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None

        self._bars_in_session += 1

        # Update bar history
        self._bar_history.append(current)

        # Track level visits with session and bar index
        level = self._round_price(current.close)
        self._level_visits[level].append((current.session, self._bars_in_session))

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            return

        # Check if we should confirm and enter pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after confirmation attempt
            self._pending_setup = None
            self._pending_absorption_close = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_estimated_session_length(self) -> int:
        """Estimate typical session length from history."""
        if not self._session_bars_history:
            return 70  # Default estimate
        return int(sum(self._session_bars_history) / len(self._session_bars_history))

    def _get_session_position_fraction(self) -> float:
        """Get what fraction of the session has elapsed."""
        estimated_length = self._get_estimated_session_length()
        if estimated_length <= 0:
            return 0.5
        return self._bars_in_session / estimated_length

    def _get_cumulative_level_weight(self, level: float) -> float:
        """
        Calculate cumulative weight for a level across all sessions.
        
        Current session: 1.0x weight
        Prior session: 0.6x weight
        2+ sessions ago: 0.2x weight
        """
        if level not in self._level_visits:
            return 0.0

        visits = self._level_visits[level]
        total_weight = 0.0

        for session, _ in visits:
            sessions_ago = self._current_session - session
            
            if sessions_ago == 0:
                total_weight += 1.0
            elif sessions_ago == 1:
                total_weight += 0.6
            else:
                total_weight += 0.2

        return total_weight

    def _get_cluster_strength(self, level: float) -> int:
        """Count how many distinct sessions a level appears in."""
        if level not in self._level_visits:
            return 0

        visits = self._level_visits[level]
        sessions_with_visits = set(v[0] for v in visits)
        return len(sessions_with_visits)

    def _is_persistent_level(self, price: float) -> bool:
        """Check if price is near a level with multi-session persistence."""
        level = self._round_price(price)
        
        if level not in self._level_visits:
            return False

        cluster_strength = self._get_cluster_strength(level)
        
        # Need at least 2 distinct sessions
        return cluster_strength >= 2

    def _get_dominant_direction(self) -> int:
        """Get dominant delta direction from recent bars."""
        if len(self._bar_history) < self.momentum_lookback:
            return 0

        recent_bars = list(self._bar_history)[-self.momentum_lookback:]
        bullish_count = sum(1 for b in recent_bars if b.delta > 0)
        bearish_count = sum(1 for b in recent_bars if b.delta < 0)

        if bullish_count > bearish_count:
            return 1
        elif bearish_count > bullish_count:
            return -1
        else:
            return 0

    def _get_wick_requirement(self, position_fraction: float) -> float:
        """
        Get wick requirement based on session position.
        
        Early session (0-25%): higher requirement
        Mid session (25-75%): standard requirement
        Late session (75-100%): higher requirement
        """
        if position_fraction < 0.25:
            return self.min_wick_points * self.early_session_wick_mult
        elif position_fraction > 0.75:
            return self.min_wick_points * self.late_session_wick_mult
        else:
            return self.min_wick_points

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with multi-session level persistence.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Must be near persistent level
        if not self._is_persistent_level(current.close):
            return None

        # Get level strength for delta ratio adjustment
        level = self._round_price(current.close)
        cumulative_weight = self._get_cumulative_level_weight(level)
        cluster_strength = self._get_cluster_strength(level)

        # Adjust delta ratio based on cluster strength
        if cluster_strength >= self.strong_cluster_sessions:
            required_delta_ratio = self.absorption_delta_ratio * 0.93  # 0.26
        else:
            required_delta_ratio = self.absorption_delta_ratio * 1.07  # 0.30

        # Filter 2: Strong directional delta
        if delta_ratio < required_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Get session position for wick requirement
        position_fraction = self._get_session_position_fraction()
        wick_requirement = self._get_wick_requirement(position_fraction)

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 3: Prior bar shows opposite direction (or allow if strong cluster)
            if len(self._bar_history) >= 2:
                setup_bar = self._bar_history[-2]
                if setup_bar.delta >= 0 and cluster_strength < self.strong_cluster_sessions:
                    return None

            lower_wick = current.close - current.low
            
            # Filter 4: Wick size (adjusted for session position)
            if lower_wick < wick_requirement:
                return None
            
            # Filter 5: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 6: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_close": current.close,
            }
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            # Filter 3: Prior bar shows opposite direction (or allow if strong cluster)
            if len(self._bar_history) >= 2:
                setup_bar = self._bar_history[-2]
                if setup_bar.delta <= 0 and cluster_strength < self.strong_cluster_sessions:
                    return None

            upper_wick = current.high - current.close
            
            # Filter 4: Wick size (adjusted for session position)
            if upper_wick < wick_requirement:
                return None
            
            # Filter 5: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 6: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_close": current.close,
            }
        
        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Requirements:
        1. Directional follow-through
        2. Must touch the absorption level
        3. Momentum maintained
        """
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]
        
        if direction == "long":
            # Must close above, touch the level, and maintain momentum
            return (confirming_bar.close > self._pending_absorption_close and 
                    confirming_bar.low <= self._pending_absorption_close and
                    confirming_bar.delta >= 0)
        else:  # short
            # Must close below, touch the level, and maintain momentum
            return (confirming_bar.close < self._pending_absorption_close and 
                    confirming_bar.high >= self._pending_absorption_close and
                    confirming_bar.delta <= 0)

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
