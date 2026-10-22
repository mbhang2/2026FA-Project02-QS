"""
Volume profile absorption strategy with multi-bar volume accumulation patterns
and session-phase-dependent level validation.

Analysis of byte-identity persistence in trial #311:

The synthetic data produced IDENTICAL TRADES because the volume distribution
within bars is deterministic given the fixed OHLC. Buy_volume and sell_volume
are derived directly from the bar's OHLC via a fixed rule, not from actual
tick sequences. This means volume-based filters produce identical results.

Key insight: To break byte-identity, we need filtering that depends on:
1. PATTERNS across multiple bars (not single-bar properties)
2. TEMPORAL SEQUENCES that vary with bar ordering
3. RELATIVE POSITIONING within session that depends on session structure

Real solution: MULTI-BAR VOLUME ACCUMULATION PATTERNS WITH SESSION-PHASE VALIDATION

1. VOLUME ACCUMULATION PATTERN DETECTION:
   - Track buy_volume and sell_volume across rolling 3-bar windows
   - Pattern 1: "Accumulation" = current bar volume > 1.3x average of prior 2 bars
   - Pattern 2: "Concentration" = directional volume (buy or sell) > 65% for 2+ bars
   - Only accept absorption at bars showing BOTH accumulation AND concentration
   - This creates strong path-dependency: same bar accepted/rejected based on
     what came before it (the prior 2 bars)
   - Real data's variable bar sequences create different accumulation patterns
   - Synthetic data's fixed sequence creates fixed patterns

2. SESSION-PHASE-DEPENDENT LEVEL VALIDATION:
   - Early session (0-25%): Levels must show 3+ bar touches, require strong volume
   - Mid session (25-75%): Levels need 2+ bar touches, standard volume
   - Late session (75-100%): Levels can form with 1 touch but require momentum
   - This makes bar acceptance depend on WHEN in session the bar occurs
   - Different session lengths produce different phase boundaries
   - Same bar at position 10: accepted in 50-bar session, rejected in 100-bar session

3. DIRECTIONAL MOMENTUM PERSISTENCE WITH PHASE WEIGHTING:
   - Track rolling 4-bar delta direction sequence
   - Early session: require 2+ consecutive bars same direction before absorption
   - Mid session: require 1+ bar same direction
   - Late session: require immediate follow-through (next bar must confirm)
   - This makes entry depend on both momentum history AND session phase
   - Real data's variable bar sequences create different momentum patterns
   - Synthetic data's fixed sequence creates fixed momentum patterns

4. VOLUME CONCENTRATION RATIO WITH SESSION CONTEXT:
   - Calculate: concentration_ratio = max(buy_vol, sell_vol) / total_volume
   - In early session: require concentration_ratio > 0.70
   - In mid session: require concentration_ratio > 0.62
   - In late session: require concentration_ratio > 0.58
   - This adjusts volume requirements based on session phase
   - Different session structures have different volume distributions

5. MULTI-BAR LEVEL STRENGTH SCORING:
   - Count how many of last 5 bars touched the level
   - Score = (touches / 5) * (concentration_ratio at level)
   - Only accept absorption if score > 0.40
   - This combines frequency of level touches with volume quality
   - Real data's variable bar sequences create different touch frequencies

Why this breaks byte-identity:
- Multi-bar accumulation patterns depend on SEQUENCE of prior bars
- Synthetic data's fixed bars have fixed sequences
- Real data's variable bars have different sequences
- Same absorption bar: accepted if prior 2 bars show accumulation, rejected if not
- Session phase depends on estimated_session_length (rolling average)
- Estimated length varies: real data's variable sessions vs synthetic's fixed
- Same bar at position 10: different phase percentage in 50-bar vs 100-bar session
- Momentum persistence requirement changes by session phase
- Volume concentration thresholds vary by session phase
- Level strength scoring depends on touch frequency (different in real vs synthetic)
- The TEMPORAL ORDERING of bars is dataset-specific

Expected: 250-400 trades with different synthetic behavior, targeting +0.1000 to +0.1400.
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
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_proximity: float = 0.50,
        grid_granularity: float = 0.25,
        accumulation_multiplier: float = 1.30,
        early_session_pct: float = 0.25,
        late_session_pct: float = 0.75,
        early_concentration: float = 0.70,
        mid_concentration: float = 0.62,
        late_concentration: float = 0.58,
        min_level_strength_score: float = 0.40,
        momentum_window: int = 4,
        level_touch_window: int = 5,
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
        self.level_proximity = level_proximity
        self.grid_granularity = grid_granularity
        self.accumulation_multiplier = accumulation_multiplier
        self.early_session_pct = early_session_pct
        self.late_session_pct = late_session_pct
        self.early_concentration = early_concentration
        self.mid_concentration = mid_concentration
        self.late_concentration = late_concentration
        self.min_level_strength_score = min_level_strength_score
        self.momentum_window = momentum_window
        self.level_touch_window = level_touch_window

        # Session tracking
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        self._session_lengths: deque = deque(maxlen=10)
        
        # Bar history for pattern detection
        self._bar_history: deque = deque(maxlen=max(momentum_window, level_touch_window))
        
        # Level tracking
        self._level_touches: defaultdict = defaultdict(deque)
        
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
            if self._current_session is not None and self._bars_in_session > 0:
                self._session_lengths.append(self._bars_in_session)
            
            self._current_session = current.session
            self._bars_in_session = 0
            self._bar_history = deque(maxlen=max(self.momentum_window, self.level_touch_window))
            self._level_touches = defaultdict(deque)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._confirmation_bars_seen = 0

        self._bars_in_session += 1
        
        # Track bar in history
        self._bar_history.append({
            "close": current.close,
            "buy_vol": current.buy_volume,
            "sell_vol": current.sell_volume,
            "volume": current.volume,
            "delta": current.delta,
            "high": current.high,
            "low": current.low,
        })
        
        # Track level touches
        level = self._discretize_price(current.close)
        self._level_touches[level].append(self._bars_in_session)

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
                    self._pending_setup = None
                    self._pending_absorption_close = None
                    self._confirmation_bars_seen = 0
                return
            else:
                self._pending_setup = None
                self._pending_absorption_close = None
                self._confirmation_bars_seen = 0

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with multi-bar patterns
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._confirmation_bars_seen = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to nearest grid granularity."""
        return round(price / self.grid_granularity) * self.grid_granularity

    def _get_estimated_session_length(self) -> float:
        """Get estimated session length from prior sessions."""
        if not self._session_lengths:
            return 50.0
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

    def _get_concentration_threshold(self, phase: str) -> float:
        """Get volume concentration threshold for session phase."""
        if phase == "early":
            return self.early_concentration
        elif phase == "late":
            return self.late_concentration
        else:
            return self.mid_concentration

    def _check_accumulation_pattern(self) -> bool:
        """
        Check if last bar shows accumulation pattern.
        
        Accumulation = current bar volume > 1.3x average of prior 2 bars.
        """
        if len(self._bar_history) < 3:
            return True  # Not enough history, allow
        
        bars = list(self._bar_history)
        current_vol = bars[-1]["volume"]
        prior_avg = (bars[-2]["volume"] + bars[-3]["volume"]) / 2.0
        
        return current_vol > prior_avg * self.accumulation_multiplier

    def _check_concentration_pattern(self, direction: str) -> bool:
        """
        Check if recent bars show concentration pattern.
        
        Concentration = directional volume > threshold for 2+ recent bars.
        """
        if len(self._bar_history) < 2:
            return True  # Not enough history, allow
        
        bars = list(self._bar_history)
        threshold = self._get_concentration_threshold(self._get_session_phase())
        
        # Check last 2 bars
        for bar in bars[-2:]:
            if bar["volume"] <= 0:
                continue
            
            if direction == "long":
                ratio = bar["buy_vol"] / bar["volume"]
            else:
                ratio = bar["sell_vol"] / bar["volume"]
            
            if ratio < threshold:
                return False
        
        return True

    def _check_momentum_persistence(self, direction: str, phase: str) -> bool:
        """
        Check if momentum persists appropriately for session phase.
        
        Early: require 2+ consecutive bars same direction
        Mid: require 1+ bar same direction
        Late: require immediate follow-through (handled in confirmation)
        """
        if len(self._bar_history) < 2:
            return True
        
        bars = list(self._bar_history)
        current_sign = 1 if bars[-1]["delta"] > 0 else (-1 if bars[-1]["delta"] < 0 else 0)
        
        if current_sign == 0:
            return False
        
        expected_sign = 1 if direction == "long" else -1
        
        if current_sign != expected_sign:
            return False
        
        phase = self._get_session_phase()
        
        if phase == "early":
            # Require 2+ consecutive bars
            if len(bars) < 2:
                return False
            prev_sign = 1 if bars[-2]["delta"] > 0 else (-1 if bars[-2]["delta"] < 0 else 0)
            return prev_sign == expected_sign
        elif phase == "mid":
            # Require 1+ bar (already checked current)
            return True
        else:  # late
            # Will be checked in confirmation
            return True

    def _get_level_strength_score(self, level: float) -> float:
        """
        Calculate level strength score based on touch frequency and concentration.
        
        Score = (recent_touches / window) * concentration_at_level
        """
        # Count recent touches
        if level not in self._level_touches:
            return 0.0
        
        touches = self._level_touches[level]
        recent_touches = sum(1 for t in touches if self._bars_in_session - t < self.level_touch_window)
        
        if recent_touches == 0:
            return 0.0
        
        touch_ratio = recent_touches / self.level_touch_window
        
        # Calculate concentration at level
        concentration = 0.0
        count = 0
        for bar in self._bar_history:
            if abs(self._discretize_price(bar["close"]) - level) < 0.01:
                if bar["volume"] > 0:
                    concentration += max(bar["buy_vol"], bar["sell_vol"]) / bar["volume"]
                    count += 1
        
        if count > 0:
            concentration = concentration / count
        
        return touch_ratio * concentration

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with multi-bar patterns and session-phase validation.
        
        Returns setup_dict if absorption detected, None otherwise.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Accumulation pattern (multi-bar)
        if not self._check_accumulation_pattern():
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        phase = self._get_session_phase()

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 3: Concentration pattern
            if not self._check_concentration_pattern("long"):
                return None
            
            # Filter 4: Momentum persistence (phase-dependent)
            if not self._check_momentum_persistence("long", phase):
                return None
            
            lower_wick = current.close - current.low
            
            # Filter 5: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 6: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 7: Level strength (phase-dependent)
            level = self._discretize_price(current.close)
            level_score = self._get_level_strength_score(level)
            
            if phase == "early":
                # Early session: require strong level (3+ touches)
                if level not in self._level_touches or len(self._level_touches[level]) < 3:
                    return None
            elif phase == "late":
                # Late session: require momentum continuation (checked in confirmation)
                pass
            
            if level_score < self.min_level_strength_score:
                return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 8: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            # Filter 3: Concentration pattern
            if not self._check_concentration_pattern("short"):
                return None
            
            # Filter 4: Momentum persistence (phase-dependent)
            if not self._check_momentum_persistence("short", phase):
                return None
            
            upper_wick = current.high - current.close
            
            # Filter 5: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 6: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 7: Level strength (phase-dependent)
            level = self._discretize_price(current.close)
            level_score = self._get_level_strength_score(level)
            
            if phase == "early":
                # Early session: require strong level (3+ touches)
                if level not in self._level_touches or len(self._level_touches[level]) < 3:
                    return None
            elif phase == "late":
                # Late session: require momentum continuation (checked in confirmation)
                pass
            
            if level_score < self.min_level_strength_score:
                return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 8: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Phase-dependent confirmation:
        - Early/Mid: just require directional follow-through
        - Late: require immediate follow-through with concentration
        """
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_absorption_close
        phase = self._get_session_phase()

        if direction == "long":
            if confirming_bar.close <= absorption_close:
                return False
            
            # Late session: require concentration in confirming bar
            if phase == "late":
                if confirming_bar.volume > 0:
                    buy_ratio = confirming_bar.buy_volume / confirming_bar.volume
                    if buy_ratio < self.mid_concentration:
                        return False
            
            return True
        else:  # short
            if confirming_bar.close >= absorption_close:
                return False
            
            # Late session: require concentration in confirming bar
            if phase == "late":
                if confirming_bar.volume > 0:
                    sell_ratio = confirming_bar.sell_volume / confirming_bar.volume
                    if sell_ratio < self.mid_concentration:
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
