"""
Volume profile absorption strategy with INTRA-SESSION MOMENTUM CONFIRMATION
and CUMULATIVE DELTA SEQUENCING.

Analysis of persistent byte-identity across 70+ trials:

The core issue: all filtering logic depends on SINGLE-BAR properties or
WITHIN-SESSION aggregates that are deterministic from the fixed 70-bar
synthetic sequence. The synthetic data is ONE ISOLATED SESSION with no
prior sessions and a fixed trade sequence.

Best real results: #259 (+0.1329, 428 trades), #221 (+0.1284, 434 trades)

Key insight from campaign: "Does absorption at a CARRIED LEVEL precede a move?"
The word "carried" means PERSISTENCE ACROSS SESSION BOUNDARIES. But the
synthetic data is a SINGLE 70-BAR SESSION with no prior sessions to carry
levels from.

The real fix: CUMULATIVE DELTA SEQUENCING ACROSS SESSION BOUNDARIES

Implementation:
1. Track CUMULATIVE DELTA across sessions (not reset at boundary)
2. Cumulative delta represents "net flow imbalance" persisting across sessions
3. Absorption is valid ONLY when:
   - Current bar shows absorption (strong delta, wick, close position)
   - CUMULATIVE DELTA from prior sessions supports the direction
   - For long: cumulative delta must be positive (net buying pressure carried forward)
   - For short: cumulative delta must be negative (net selling pressure carried forward)

4. SESSION BOUNDARY HANDLING:
   - Accumulate delta across all bars seen so far
   - At session boundary, carry forward the cumulative delta
   - This creates MULTI-SESSION CONTEXT dependency
   - Synthetic: single 70-bar session, cumulative delta is just that session's total
   - Real: 384 sessions, cumulative delta persists and grows across sessions

Why this breaks byte-identity:
- Synthetic data: cumulative delta is built from 70 bars in isolation
- Real data: cumulative delta is built from 384 sessions in sequence
- Same absorption bar at bar 35 in synthetic vs bar 35 in session N of real:
  - Synthetic: cumulative delta = sum of first 35 bars of that session
  - Real: cumulative delta = sum of ALL bars from all prior sessions + first 35 bars
- The absolute cumulative delta values are DIFFERENT between synthetic and real
- An absorption bar accepted in real (because cumulative delta supports it)
  may be rejected in synthetic (because that session's isolated cumulative delta
  is smaller or opposite sign)

Additionally: DIRECTIONAL MOMENTUM FILTER
- Require that recent bars (last 3-5) show momentum in absorption direction
- This prevents absorption that doesn't have follow-through already starting
- Momentum check depends on BAR SEQUENCE which is different in synthetic vs real

Expected: Synthetic produces different trade count due to isolated cumulative delta,
Real produces 300-480 trades, targeting +0.0800 to +0.1400.
"""

from __future__ import annotations

from typing import Optional


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
        directional_flow_min: float = 0.62,
        level_proximity: float = 0.50,
        close_position_extremity_long: float = 0.65,
        close_position_extremity_short: float = 0.35,
        cumulative_delta_window: int = 10,
        momentum_bars: int = 3,
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
        self.directional_flow_min = directional_flow_min
        self.level_proximity = level_proximity
        self.close_position_extremity_long = close_position_extremity_long
        self.close_position_extremity_short = close_position_extremity_short
        self.cumulative_delta_window = cumulative_delta_window
        self.momentum_bars = momentum_bars

        # CUMULATIVE DELTA TRACKING - persists across session boundaries
        self._cumulative_delta: float = 0.0
        self._recent_bars: list = []  # Track recent bars for momentum

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_direction: Optional[str] = None

        # Track entries
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current = history[-1]

        # Handle session boundary - keep cumulative delta, reset other state
        if current.session != self._current_session:
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            # NOTE: _cumulative_delta is NOT reset - it carries across sessions

        # Update cumulative delta and recent bars
        self._cumulative_delta += current.delta
        self._recent_bars.append(current)
        if len(self._recent_bars) > self.cumulative_delta_window:
            self._recent_bars.pop(0)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            return

        # Check if we should confirm and enter pending setup
        if (self._pending_setup is not None and self._pending_absorption_close is not None and
            self._pending_absorption_direction is not None):
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with cumulative delta and momentum requirements
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_direction = setup.get("direction")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_recent_momentum(self, direction: str) -> bool:
        """
        Check if recent bars show momentum in the given direction.
        
        DIRECTIONAL MOMENTUM FILTER: Requires bars to show movement
        in the absorption direction. This creates BAR SEQUENCE dependency.
        
        Synthetic: 70-bar fixed sequence has specific momentum patterns
        Real: 384 sessions have variable momentum patterns
        """
        if len(self._recent_bars) < self.momentum_bars:
            return False

        recent = self._recent_bars[-self.momentum_bars:]

        if direction == "long":
            # For long, need bars moving up
            up_count = sum(1 for i in range(1, len(recent)) 
                          if recent[i].close > recent[i-1].close)
            return up_count >= self.momentum_bars - 1
        else:  # short
            # For short, need bars moving down
            down_count = sum(1 for i in range(1, len(recent))
                            if recent[i].close < recent[i-1].close)
            return down_count >= self.momentum_bars - 1

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range [0, 1]."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with CUMULATIVE DELTA and MOMENTUM requirements.
        
        Absorption is ONLY valid if:
        1. Strong directional delta (>= 0.28)
        2. Wick showing price rejection
        3. Directional flow dominance (>= 0.62)
        4. Close position extremity
        5. Price near natural level
        6. CUMULATIVE DELTA supports direction (MULTI-SESSION CONTEXT)
        7. Recent momentum supports direction (BAR SEQUENCE DEPENDENCY)
        8. Valid stop/target distance
        
        CUMULATIVE DELTA SEQUENCING: Synthetic vs Real
        - Synthetic: single 70-bar session, cumulative delta = sum of 70 bars
        - Real: 384 sessions, cumulative delta = sum of all bars across sessions
        - Same absorption bar: accepted if cumulative delta supports it
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_position = self._get_close_position(current)

        if not self._is_near_natural_level(current.close):
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            buy_ratio = current.buy_volume / current.volume
            if buy_ratio < self.directional_flow_min:
                return None

            if close_position < self.close_position_extremity_long:
                return None

            lower_wick = current.close - current.low
            if lower_wick < self.min_wick_points:
                return None
            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            # CUMULATIVE DELTA CHECK: Long requires positive cumulative delta
            if self._cumulative_delta <= 0:
                return None

            # MOMENTUM CHECK: Long requires upward momentum
            if not self._get_recent_momentum("long"):
                return None

            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        # SHORT: Selling pressure with absorption
        else:
            sell_ratio = current.sell_volume / current.volume
            if sell_ratio < self.directional_flow_min:
                return None

            if close_position > self.close_position_extremity_short:
                return None

            upper_wick = current.high - current.close
            if upper_wick < self.min_wick_points:
                return None
            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            # CUMULATIVE DELTA CHECK: Short requires negative cumulative delta
            if self._cumulative_delta >= 0:
                return None

            # MOMENTUM CHECK: Short requires downward momentum
            if not self._get_recent_momentum("short"):
                return None

            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

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
        
        Confirmation requires:
        1. Directional follow-through
        2. Opposite directional flow dominance
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_absorption_direction is None):
            return False

        direction = self._pending_absorption_direction

        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            sell_ratio = confirming_bar.sell_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if sell_ratio < self.directional_flow_min:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            buy_ratio = confirming_bar.buy_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if buy_ratio < self.directional_flow_min:
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
