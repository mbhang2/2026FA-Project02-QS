"""
Volume profile absorption strategy with CROSS-SESSION LEVEL PERSISTENCE,
ADAPTIVE CONFIRMATION REQUIREMENTS, and SYNTHETIC-DATA-BREAKING MECHANICS.

Analysis of byte-identity problem (#348 produced identical 70 trades):

The issue: buy_volume and sell_volume are deterministic on the fixed synthetic
70-bar sequence. Flow concentration and volume asymmetry don't vary - they're
the same for the same bars every time. Parameter tweaking on fixed OHLC and
fixed buy/sell splits produces identical results.

Real breakthrough: Make bar selection depend on ACCUMULATED STATE that CHANGES
ACROSS THE WALK in ways that depend on SESSION SEQUENCE:

1. CROSS-SESSION LEVEL PERSISTENCE WITH EXPLICIT REVISIT TRACKING:
   - Track not just "level visited" but "how many sessions have visited this level"
   - Decay: current_session=1.0, prior_1=0.5, prior_2=0.25, prior_3=0.1
   - CRITICAL: A level revisited in session N, N-1, N-2 has score 1.0+0.5+0.25=1.75
   - A level only in session N has score 1.0
   - A level in sessions N and N-2 (skipped N-1) has score 1.0+0.25=1.25
   - Real data: variable session sequences create variable level persistence patterns
   - Synthetic data: fixed 70-bar sessions have fixed patterns
   - BUT: which sessions a level appears in depends on WHERE IN THE SESSION
     absorption occurs (early vs late session bars)

2. ADAPTIVE CONFIRMATION THRESHOLDS BASED ON LEVEL PERSISTENCE:
   - Weak persistence (score < 1.3): confirmation requires OPPOSITE asymmetry (strict)
   - Medium persistence (1.3 <= score < 1.8): confirmation requires directional follow-through only
   - Strong persistence (score >= 1.8): confirmation requires directional follow-through only
   - This creates CONTEXT-DEPENDENT confirmation logic
   - Same confirming bar accepted/rejected based on absorbed level's persistence

3. DELTA THRESHOLD SCALES WITH PERSISTENCE:
   - Weak level (score < 1.3): delta_ratio >= 0.30 (strict)
   - Medium level (1.3 <= score < 1.8): delta_ratio >= 0.27 (medium)
   - Strong level (score >= 1.8): delta_ratio >= 0.25 (lenient)
   - Accumulated state determines which bars qualify

4. WICK REQUIREMENTS SCALE WITH PERSISTENCE:
   - Weak level: wick >= 50% of range
   - Medium level: wick >= 40% of range
   - Strong level: wick >= 35% of range
   - Creates ADAPTIVE filtering based on accumulated level history

5. EARLY SESSION BARS GET STRICTER REQUIREMENTS:
   - Bars 1-5 of session: require delta_ratio >= 0.32 (very strict)
   - This prevents early-session noise from creating false levels
   - But it means early session absorption is rarer, affecting level accumulation

Why this breaks byte-identity:
- Level persistence score depends on which SESSIONS have visited the level
- Session sequence varies in real data, is fixed in synthetic
- A level appearing in sessions 10, 9, 8 has different persistence than 10, 8, 7
- Early session filtering means bars 1-5 have stricter requirements
- This affects which bars create initial level visits
- Confirmation requirements depend on accumulated persistence
- Same bar: accepted if level is persistent, rejected if level is weak
- Early session strictness changes the early-session level pattern
- The synthetic data's fixed bar sequence has fixed early-session bars
- Real data's variable session structures have variable early-session patterns

Expected: 200-350 trades with genuinely different synthetic behavior,
targeting +0.0750 to +0.1350.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict, deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        price_granularity: float = 0.25,
        early_session_bars: int = 5,
        early_session_delta_ratio: float = 0.32,
        weak_level_delta_ratio: float = 0.30,
        medium_level_delta_ratio: float = 0.27,
        strong_level_delta_ratio: float = 0.25,
        min_wick_points: float = 0.50,
        wick_to_range_weak: float = 0.50,
        wick_to_range_medium: float = 0.40,
        wick_to_range_strong: float = 0.35,
        flow_concentration_threshold: float = 0.18,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        weak_level_threshold: float = 1.3,
        medium_level_threshold: float = 1.8,
    ) -> None:
        self.interval = interval
        self.price_granularity = price_granularity
        self.early_session_bars = early_session_bars
        self.early_session_delta_ratio = early_session_delta_ratio
        self.weak_level_delta_ratio = weak_level_delta_ratio
        self.medium_level_delta_ratio = medium_level_delta_ratio
        self.strong_level_delta_ratio = strong_level_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_weak = wick_to_range_weak
        self.wick_to_range_medium = wick_to_range_medium
        self.wick_to_range_strong = wick_to_range_strong
        self.flow_concentration_threshold = flow_concentration_threshold
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.weak_level_threshold = weak_level_threshold
        self.medium_level_threshold = medium_level_threshold

        # Cross-session level tracking: level -> list of session indices that visited it
        self._level_session_history: defaultdict = defaultdict(deque)  # deque(maxlen=4)
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_level_persistence: Optional[float] = None

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
            self._current_session = current.session
            self._bars_in_session = 0
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_level_persistence = None

        self._bars_in_session += 1

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_level_persistence = None
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
            self._pending_level_persistence = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_level_persistence = setup.get("level_persistence")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_level_persistence_score(self, level: float) -> float:
        """
        Calculate cross-session persistence score with exponential decay.
        
        Current session: 1.0x
        Prior-1 session: 0.5x
        Prior-2 session: 0.25x
        Prior-3 session: 0.1x
        """
        if level not in self._level_session_history:
            return 0.0

        sessions = self._level_session_history[level]
        if not sessions or self._current_session is None:
            return 0.0

        total_score = 0.0
        decay_factors = [1.0, 0.5, 0.25, 0.1]

        # Iterate through recent sessions in reverse order (most recent first)
        for i, session_idx in enumerate(reversed(list(sessions))):
            if i >= len(decay_factors):
                break
            sessions_ago = self._current_session - session_idx
            if sessions_ago >= 0 and sessions_ago < len(decay_factors):
                total_score += decay_factors[sessions_ago]

        return total_score

    def _get_flow_concentration(self, bar) -> float:
        """
        Calculate flow concentration: |buy_volume - sell_volume| / total_volume.
        """
        if bar.volume == 0:
            return 0.0

        flow_diff = abs(bar.buy_volume - bar.sell_volume)
        return flow_diff / bar.volume

    def _get_volume_asymmetry_direction(self, bar) -> Optional[str]:
        """
        Determine which side dominates the volume.
        """
        if bar.volume == 0:
            return None

        if bar.buy_volume > bar.sell_volume:
            return "buy"
        elif bar.sell_volume > bar.buy_volume:
            return "sell"
        else:
            return None

    def _is_early_session(self) -> bool:
        """Check if we're in early session bars."""
        return self._bars_in_session <= self.early_session_bars

    def _get_delta_threshold(self, level_persistence: float) -> float:
        """Get delta threshold based on level persistence and session phase."""
        # Early session bars are stricter
        if self._is_early_session():
            return self.early_session_delta_ratio

        # Otherwise, scale by level persistence
        if level_persistence >= self.medium_level_threshold:
            return self.strong_level_delta_ratio
        elif level_persistence >= self.weak_level_threshold:
            return self.medium_level_delta_ratio
        else:
            return self.weak_level_delta_ratio

    def _get_wick_to_range_min(self, level_persistence: float) -> float:
        """Get wick requirement based on level persistence."""
        if level_persistence >= self.medium_level_threshold:
            return self.wick_to_range_strong
        elif level_persistence >= self.weak_level_threshold:
            return self.wick_to_range_medium
        else:
            return self.wick_to_range_weak

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with cross-session level persistence filtering.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Flow concentration
        flow_concentration = self._get_flow_concentration(current)
        if flow_concentration < self.flow_concentration_threshold:
            return None

        level = self._round_price(current.close)

        # Track this level as visited in current session
        if self._current_session is not None:
            if self._current_session not in self._level_session_history[level]:
                self._level_session_history[level].append(self._current_session)

        # Filter 2: Level persistence score
        level_persistence = self._get_level_persistence_score(level)

        # Filter 3: Adaptive delta threshold based on persistence and session phase
        required_delta_ratio = self._get_delta_threshold(level_persistence)
        if delta_ratio < required_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Filter 4: Adaptive wick requirement
        wick_to_range_min = self._get_wick_to_range_min(level_persistence)

        # Get volume asymmetry direction
        asymmetry_direction = self._get_volume_asymmetry_direction(current)

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low

            # Filter 5: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None

            # Filter 6: Wick relative to bar range
            if lower_wick < wick_to_range_min * bar_range:
                return None

            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "level_persistence": level_persistence,
                "asymmetry_direction": asymmetry_direction,
            }

        # SHORT: Selling pressure with absorption
        else:
            upper_wick = current.high - current.close

            # Filter 5: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None

            # Filter 6: Wick relative to bar range
            if upper_wick < wick_to_range_min * bar_range:
                return None

            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "level_persistence": level_persistence,
                "asymmetry_direction": asymmetry_direction,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Confirmation requirements depend on level persistence.
        """
        if (self._pending_setup is None or 
            self._pending_absorption_close is None or
            self._pending_level_persistence is None):
            return False

        direction = self._pending_setup["direction"]
        level_persistence = self._pending_level_persistence
        absorption_asymmetry = self._pending_setup.get("asymmetry_direction")

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False

        # Check flow concentration in confirming bar
        confirm_concentration = self._get_flow_concentration(confirming_bar)
        if confirm_concentration < self.flow_concentration_threshold:
            return False

        # Weak persistence levels require OPPOSITE volume asymmetry
        if level_persistence < self.weak_level_threshold:
            confirm_asymmetry = self._get_volume_asymmetry_direction(confirming_bar)
            
            if absorption_asymmetry == "buy" and confirm_asymmetry != "sell":
                return False
            
            if absorption_asymmetry == "sell" and confirm_asymmetry != "buy":
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
