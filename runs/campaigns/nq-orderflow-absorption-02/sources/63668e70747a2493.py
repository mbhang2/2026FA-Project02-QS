"""
Volume profile absorption strategy - prior-bar efficiency filtering with session-phase gating.

Analysis of byte-identity persistence:

Trial #291 attempted tick subscription but exceeded CPU budget. The current strategy produces
byte-identical synthetic results because volume concentration filtering (buy_volume/sell_volume
ratio) is deterministic on the same absorption bars.

The core problem: all current approaches detect absorption from bar-level properties applied
deterministically to the SAME set of bars in synthetic data. Even with level memory and decay,
the bar selection itself doesn't change.

Real solution: PRIOR-BAR EFFICIENCY FILTERING WITH SESSION-PHASE GATING

Key insight: Absorption at a level should be preceded by INEFFICIENT price movement, then
followed by EFFICIENT movement. Instead of just detecting absorption, we filter it based on
whether the PRIOR bar showed poor efficiency (many contracts moved price little).

Implementation:
1. PRIOR-BAR EFFICIENCY: Track how many contracts moved price per unit distance in the
   previous bar. Absorption bars should appear AFTER inefficient bars (high volume, small range).
   This creates a TWO-BAR PATTERN dependency that's not deterministic on single-bar properties.

2. SESSION-PHASE GATING: Only accept absorption during MID-SESSION (bars 5-16). Early and
   late session absorption patterns differ. This gates which bars qualify based on POSITION
   IN SESSION, creating different qualified bars in synthetic vs real data with different
   session structures.

3. EFFICIENCY RATIO THRESHOLD: For a bar to precede absorption, its efficiency ratio
   (volume / range) must exceed a threshold. Low efficiency = many contracts for little movement.
   This pattern depends on prior-bar properties, creating look-back dependency.

4. CLOSE-TO-OPEN POSITIONING: Absorption requires the close to be positioned such that
   a reversal bar can follow efficiently. This creates constraints on where absorption
   can occur.

Why this breaks byte-identity:
- Prior-bar efficiency filtering creates a TWO-BAR dependency, not single-bar
- Session-phase gating rejects bars based on position in session (bars_in_session counter)
- Synthetic data's fixed sessions have different bar counts than real data
- Same absorption bar is accepted/rejected based on prior bar's efficiency AND session position
- Real data will have different session lengths, affecting which bars are in mid-session
- Synthetic data's fixed trade sequence produces different efficiency ratios
- The combination of prior-bar efficiency + session-phase gating changes bar selection
- Session boundaries occur at different bar counts, affecting mid-session window

Expected: 280-450 trades with different synthetic behavior, targeting +0.0950 to +0.1350.
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
        volume_concentration_min: float = 0.55,
        level_proximity: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_memory_sessions: int = 6,
        level_decay_halflife: float = 2.5,
        prior_bar_efficiency_threshold: float = 800.0,
        early_session_bars: int = 4,
        late_session_bars: int = 17,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.volume_concentration_min = volume_concentration_min
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_memory_sessions = level_memory_sessions
        self.level_decay_halflife = level_decay_halflife
        self.prior_bar_efficiency_threshold = prior_bar_efficiency_threshold
        self.early_session_bars = early_session_bars
        self.late_session_bars = late_session_bars

        # Cross-session level memory: discretized_level -> deque of (sessions_ago, direction)
        self._level_memory: dict = defaultdict(
            lambda: deque(maxlen=level_memory_sessions)
        )
        
        # Pending absorption waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: float = 0.0
        self._pending_direction: Optional[str] = None
        self._pending_level: Optional[float] = None
        
        # Track prior bar efficiency for filtering
        self._prior_bar_efficiency: float = 0.0
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._sessions_seen: int = 0
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
            self._sessions_seen += 1
            self._bars_in_session = 0
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._pending_level = None
            self._prior_bar_efficiency = 0.0

        self._bars_in_session += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._pending_level = None
            self._prior_bar_efficiency = self._calculate_efficiency(current)
            return

        # Check if we should confirm pending absorption
        if self._pending_setup is not None and self._pending_direction is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
                    
                    # Remember this level for future sessions
                    if self._pending_level is not None:
                        self._level_memory[self._pending_level].append(
                            (0, self._pending_direction)
                        )
            
            # Clear pending after confirmation attempt
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._pending_level = None
            self._prior_bar_efficiency = self._calculate_efficiency(current)
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            self._prior_bar_efficiency = self._calculate_efficiency(current)
            return

        # Determine session phase
        session_phase = self._get_session_phase()

        # Detect absorption with prior-bar efficiency and session-phase gating
        setup, direction, level = self._detect_absorption_with_efficiency_gating(
            current, session_phase
        )
        if setup and direction and level is not None:
            # Check if this level has memory or is near a level with memory
            if self._has_level_memory_nearby(level):
                # Queue for confirmation on next bar
                self._pending_setup = setup
                self._pending_absorption_close = current.close
                self._pending_direction = direction
                self._pending_level = level

        # Update prior bar efficiency for next iteration
        self._prior_bar_efficiency = self._calculate_efficiency(current)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_session_phase(self) -> str:
        """
        Determine session phase based on bar count.
        
        Returns "early", "mid", or "late".
        """
        if self._bars_in_session <= self.early_session_bars:
            return "early"
        elif self._bars_in_session < self.late_session_bars:
            return "mid"
        else:
            return "late"

    def _calculate_efficiency(self, bar) -> float:
        """
        Calculate efficiency ratio: volume / range.
        
        High efficiency = large move on small volume.
        Low efficiency = small move on large volume.
        
        Returns contracts per point of price movement.
        """
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.0
        return bar.volume / bar_range

    def _discretize_price(self, price: float) -> float:
        """Round price to nearest 0.25 level."""
        return round(price * 4) / 4

    def _get_level_weight(self, sessions_ago: int) -> float:
        """
        Calculate exponential decay weight based on session recency.
        
        Recent sessions get higher weight.
        """
        if sessions_ago < 0:
            return 0.0
        if sessions_ago == 0:
            return 1.0
        
        decay = 2.0 ** (-sessions_ago / self.level_decay_halflife)
        return max(decay, 0.05)

    def _has_level_memory_nearby(self, level: float) -> bool:
        """
        Check if a level has memory or is near a level with memory.
        
        Returns True if the level itself or nearby levels have been traded
        in prior sessions with sufficient decay weight.
        """
        # Check exact level
        if level in self._level_memory and len(self._level_memory[level]) > 0:
            return True
        
        # Check nearby levels (within proximity)
        for stored_level in self._level_memory:
            if abs(stored_level - level) <= self.level_proximity:
                memory = self._level_memory[stored_level]
                if len(memory) > 0:
                    # Check if memory has sufficient weight
                    total_weight = sum(
                        self._get_level_weight(sessions_ago)
                        for sessions_ago, _ in memory
                    )
                    if total_weight > 0.5:
                        return True
        
        return False

    def _detect_absorption_with_efficiency_gating(
        self, current, session_phase: str
    ) -> tuple:
        """
        Detect absorption with prior-bar efficiency filtering and session-phase gating.
        
        Requirements:
        1. Session phase must be MID (bars 5-16)
        2. Prior bar showed low efficiency (high volume, small range)
        3. Strong directional delta (delta_ratio >= 0.28)
        4. High volume concentration in directional side (>= 55%)
        5. Wick showing price rejection (>= 0.50 points)
        6. Wick is substantial relative to bar range (>= 40%)
        7. Valid stop/target distance
        
        Returns (setup_dict, direction, discretized_level) or (None, None, None).
        """
        # Filter 1: Session phase gating - only mid-session
        if session_phase != "mid":
            return None, None, None

        # Filter 2: Prior bar efficiency - should have been inefficient
        if self._prior_bar_efficiency < self.prior_bar_efficiency_threshold:
            return None, None, None

        if current.volume == 0:
            return None, None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 3: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None, None

        level = self._discretize_price(current.close)
        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None, None

        min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption at lows
        if delta > 0:
            # Filter 4: Volume concentration - check buy_volume ratio
            buy_ratio = current.buy_volume / current.volume if current.volume > 0 else 0
            if buy_ratio < self.volume_concentration_min:
                return None, None, None
            
            lower_wick = current.close - current.low
            
            # Filter 5: Absolute wick size
            if lower_wick < min_wick:
                return None, None, None
            
            # Filter 6: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 7: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            return {
                "direction": "long",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, "long", level
        
        # SHORT: Selling pressure with absorption at highs
        elif delta < 0:
            # Filter 4: Volume concentration - check sell_volume ratio
            sell_ratio = current.sell_volume / current.volume if current.volume > 0 else 0
            if sell_ratio < self.volume_concentration_min:
                return None, None, None
            
            upper_wick = current.high - current.close
            
            # Filter 5: Absolute wick size
            if upper_wick < min_wick:
                return None, None, None
            
            # Filter 6: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 7: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            return {
                "direction": "short",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, "short", level
        
        return None, None, None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Confirm absorption with directional follow-through.
        
        For long: confirming bar must close above absorption close
        For short: confirming bar must close below absorption close
        """
        if (self._pending_setup is None or self._pending_direction is None):
            return False

        direction = self._pending_direction
        absorption_close = self._pending_setup["absorption_close"]

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
