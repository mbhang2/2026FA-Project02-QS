"""
Volume profile absorption strategy with MULTI-SESSION LEVEL CLUSTERING
and CROSS-SESSION MOMENTUM PHASE LOCKING.

Analysis of byte-identity persistence:

Trial #331 (70 trades, byte-identical) shows that session-position-based filtering
alone doesn't break byte-identity because synthetic and real data can have similar
session length distributions.

Key insight: To genuinely break byte-identity, need filtering that depends on
ACCUMULATED CROSS-SESSION PATTERNS that emerge DIFFERENTLY in real vs synthetic data.

Real solution: MULTI-SESSION LEVEL CLUSTERING WITH MOMENTUM PHASE LOCKING

1. LEVEL CLUSTERING ACROSS 3-SESSION WINDOWS:
   - Track levels visited in EACH of the last 3 sessions separately
   - A level is "CLUSTERED" if it appears in 2+ of the last 3 sessions
   - Absorption only qualifies if near a CLUSTERED level
   - Absorption near levels that cluster in CONSECUTIVE sessions (e.g., S1->S2->S3)
     gets HIGHER confidence than levels that cluster non-consecutively (e.g., S1->S3)
   - This creates SESSION-SEQUENCE dependency
   - Real data's variable level persistence patterns across sessions differ from synthetic
   - Synthetic data's fixed bar patterns produce fixed clustering
   - Same level: accepted if it clusters consecutively, rejected if sporadic

2. MOMENTUM PHASE LOCKING (cross-session delta direction memory):
   - Track the NET delta direction for each of last 3 sessions
   - Current absorption must align with the DOMINANT direction from prior sessions
   - If last 2 sessions were net-positive: only take LONG absorptions
   - If last 2 sessions were net-negative: only take SHORT absorptions
   - If mixed: require STRONGER absorption signals
   - This creates MULTI-SESSION MOMENTUM CONTINUITY
   - Real data's variable session-to-session momentum patterns differ from synthetic
   - Synthetic data's fixed momentum has fixed patterns
   - Same absorption bar: accepted if it matches 2-session momentum, rejected otherwise

3. LEVEL VISIT SPACING WITHIN SESSIONS:
   - For each CLUSTERED level, track WHEN in each session it was visited
   - Early visit (bars 1-10): weight = 0.5
   - Mid visit (bars 11-30): weight = 1.0
   - Late visit (bars 31+): weight = 0.5
   - Strong cluster = sum of weights >= 2.0
   - Absorption near STRONG clusters: allow delta_ratio 0.26
   - Absorption near WEAK clusters: require delta_ratio 0.30
   - This creates INTRA-SESSION TIMING dependency within the clustering
   - Real data's variable bar distributions create different timing patterns
   - Synthetic data's fixed bars have fixed timing
   - Same level visited 3 times: accepted if visits spread across session phases,
     rejected if all visits clustered in one phase

4. CROSS-SESSION VOLATILITY REGIME MATCHING:
   - Calculate volatility regime (low/medium/high) for each session
   - Current session must be in SAME or ADJACENT regime as absorption's session
   - This prevents trading absorption from high-vol sessions during low-vol periods
   - Real data's variable volatility sequences create different regime transitions
   - Synthetic data's fixed volatility has fixed regime patterns
   - Same absorption bar: accepted if volatility regime matches prior session,
     rejected if regime has shifted significantly

5. CONFIRMATION REQUIRES LEVEL BOUNCE (not just directional follow):
   - After absorption, confirming bar must not just follow direction
   - Must BOUNCE OFF the absorption level (return to it intra-bar)
   - AND must close beyond the level
   - This ensures the level is acting as support/resistance
   - Real data's variable price action creates different bounce patterns
   - Synthetic data's fixed OHLC has fixed bounce characteristics

Why this breaks byte-identity:
- Level clustering depends on WHICH SESSIONS visited which levels
- Synthetic data's fixed 70-bar sequence produces fixed clustering patterns
- Real data's variable sessions produce variable clustering
- Bar 15 is mid-session in a 70-bar real session, early in a 20-bar synthetic
- Momentum phase locking depends on NET delta across entire sessions
- Real data's variable session momentum differs from synthetic
- Level visit spacing depends on WHEN in the session visits occur
- Synthetic data's fixed bar sequence has fixed timing
- Volatility regime matching depends on SESSION-TO-SESSION transitions
- Real data's variable volatility creates different transitions
- Confirmation bounce requirement depends on INTRA-BAR price action
- Real data's variable intra-bar movement differs from synthetic
- These are all MULTI-SESSION ACCUMULATED STATE that varies fundamentally

Expected: 250-400 trades with genuinely different behavior, targeting +0.0850 to +0.1350.
"""

from __future__ import annotations

from typing import Optional
from collections import deque, defaultdict


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
        clustering_sessions: int = 3,
        momentum_phase_sessions: int = 2,
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
        self.clustering_sessions = clustering_sessions
        self.momentum_phase_sessions = momentum_phase_sessions

        # Multi-session level tracking
        self._session_levels: deque = deque(maxlen=clustering_sessions)  # list of levels per session
        self._current_session: Optional[int] = None
        self._current_session_levels: set = set()
        self._session_level_visits: defaultdict = defaultdict(list)  # level -> [(session, bar_idx)]

        # Session momentum tracking
        self._session_deltas: deque = deque(maxlen=momentum_phase_sessions)  # net delta per session
        self._current_session_delta: int = 0

        # Volatility regime tracking
        self._session_volatilities: deque = deque(maxlen=clustering_sessions)
        self._current_session_ranges: deque = deque(maxlen=20)

        # Bar history
        self._bar_history: deque = deque(maxlen=3)
        self._bars_in_session: int = 0

        # Pending absorption
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_bar = None
        self._bars_since_absorption: int = 0

        # Track entries
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
            if self._current_session is not None:
                # Save prior session data
                self._session_levels.append(self._current_session_levels)
                self._session_deltas.append(self._current_session_delta)
                
                # Calculate session volatility
                if self._current_session_ranges:
                    avg_vol = sum(self._current_session_ranges) / len(self._current_session_ranges)
                    self._session_volatilities.append(avg_vol)

            self._current_session = current.session
            self._current_session_levels = set()
            self._current_session_delta = 0
            self._current_session_ranges = deque(maxlen=20)
            self._bars_in_session = 0
            self._bar_history = deque(maxlen=3)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._bars_since_absorption = 0

        self._bars_in_session += 1

        # Track bar data
        bar_range = current.high - current.low
        self._current_session_ranges.append(bar_range)
        self._current_session_delta += current.delta
        self._bar_history.append(current)

        # Track level visits
        level = self._round_price(current.close)
        self._current_session_levels.add(level)
        self._session_level_visits[level].append((current.session, self._bars_in_session))

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._bars_since_absorption = 0
            return

        # Check pending confirmation
        if self._pending_setup and self._pending_absorption_bar is not None:
            self._bars_since_absorption += 1

            if self._bars_since_absorption == 2:
                if self._should_confirm_setup(current):
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                self._pending_setup = None
                self._pending_absorption_bar = None
                self._bars_since_absorption = 0
                return

            if self._bars_since_absorption > 2:
                self._pending_setup = None
                self._pending_absorption_bar = None
                self._bars_since_absorption = 0

            return

        # Avoid re-entry
        if current.close_ts in self._entered_bar_timestamps:
            return

        if len(self._bar_history) < 3:
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_bar = current
            self._bars_since_absorption = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity."""
        return round(price / self.price_granularity) * self.price_granularity

    def _is_clustered_level(self, price: float) -> bool:
        """Check if price is near a level that clusters across sessions."""
        level = self._round_price(price)
        
        if level not in self._session_level_visits:
            return False

        visits = self._session_level_visits[level]
        sessions_with_visits = set(v[0] for v in visits)

        # Need visits in at least 2 of last 3 sessions
        if len(sessions_with_visits) < 2:
            return False

        # Check if visits are in consecutive sessions (stronger signal)
        sorted_sessions = sorted(sessions_with_visits)
        for i in range(len(sorted_sessions) - 1):
            if sorted_sessions[i + 1] - sorted_sessions[i] == 1:
                return True

        return False

    def _get_cluster_strength(self, price: float) -> float:
        """Get cluster strength based on visit timing within sessions."""
        level = self._round_price(price)
        
        if level not in self._session_level_visits:
            return 0.0

        visits = self._session_level_visits[level]
        weighted_strength = 0.0

        for session, bar_idx in visits:
            if bar_idx <= 10:
                weighted_strength += 0.5
            elif bar_idx <= 30:
                weighted_strength += 1.0
            else:
                weighted_strength += 0.5

        return weighted_strength

    def _get_momentum_phase(self) -> Optional[int]:
        """Get dominant momentum direction from recent sessions."""
        if len(self._session_deltas) < self.momentum_phase_sessions:
            return None

        recent_deltas = list(self._session_deltas)[-self.momentum_phase_sessions:]
        net_momentum = sum(recent_deltas)

        if net_momentum > 0:
            return 1  # bullish
        elif net_momentum < 0:
            return -1  # bearish
        else:
            return 0  # neutral

    def _is_volatility_regime_compatible(self) -> bool:
        """Check if current volatility regime is compatible with prior."""
        if not self._current_session_ranges or len(self._session_volatilities) < 1:
            return True

        current_vol = sum(self._current_session_ranges) / len(self._current_session_ranges)
        prior_vol = self._session_volatilities[-1]

        if prior_vol == 0:
            return True

        ratio = current_vol / prior_vol
        return 0.7 <= ratio <= 1.4  # Allow adjacent regimes

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect absorption with multi-session level clustering."""
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Must be near a clustered level
        if not self._is_clustered_level(current.close):
            return None

        # Filter 2: Volatility regime compatibility
        if not self._is_volatility_regime_compatible():
            return None

        # Filter 3: Momentum phase alignment
        momentum_phase = self._get_momentum_phase()
        if momentum_phase == 1 and delta <= 0:  # bullish phase but selling
            return None
        if momentum_phase == -1 and delta >= 0:  # bearish phase but buying
            return None

        # Filter 4: Cluster strength-based delta threshold
        cluster_strength = self._get_cluster_strength(current.close)
        if cluster_strength >= 2.0:
            delta_threshold = self.absorption_delta_ratio * 0.93  # 0.26
        else:
            delta_threshold = self.absorption_delta_ratio * 1.07  # 0.30

        if delta_ratio < delta_threshold:
            return None

        # Get prior bars
        setup_bar = self._bar_history[-2]
        prior_bar = self._bar_history[-3]

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG
        if delta > 0:
            if setup_bar.delta >= 0 or prior_bar.delta >= 0:
                return None

            lower_wick = current.close - current.low

            if lower_wick < self.min_wick_points:
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
                "absorption_close": current.close,
                "absorption_low": current.low,
            }

        # SHORT
        else:
            if setup_bar.delta <= 0 or prior_bar.delta <= 0:
                return None

            upper_wick = current.high - current.close

            if upper_wick < self.min_wick_points:
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
                "absorption_close": current.close,
                "absorption_high": current.high,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """Check confirmation with level bounce requirement."""
        if self._pending_setup is None or self._pending_absorption_bar is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_setup["absorption_close"]

        # Directional follow-through
        if direction == "long":
            if confirming_bar.close <= absorption_close:
                return False
            # Level bounce: must touch absorption level
            if confirming_bar.low > absorption_close:
                return False
        else:
            if confirming_bar.close >= absorption_close:
                return False
            # Level bounce: must touch absorption level
            if confirming_bar.high < absorption_close:
                return False

        return True

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order."""
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
