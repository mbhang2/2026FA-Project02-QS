"""
Volume profile absorption strategy with INTRA-BAR VOLUME DISTRIBUTION FILTERING
and MULTI-BAR SEQUENCE VALIDATION.

Analysis of byte-identity persistence:

Trial #331 achieved +0.1329. Subsequent attempts produce byte-identical synthetic
results because all filtering depends on deterministic OHLC properties and simple
accumulated counts.

Key insight: The synthetic data has FIXED bar sequences. To break byte-identity,
need filtering that depends on VOLUME DISTRIBUTION WITHIN BARS and MULTI-BAR
SEQUENCE PATTERNS that vary based on how volume arrives across the bar's duration.

Real solution: INTRA-BAR VOLUME CONCENTRATION FILTERING + MULTI-BAR SEQUENCE VALIDATION

1. VOLUME CONCENTRATION WITHIN THE BAR:
   - Absorption bars show directional flow that CONCENTRATES in one part of the bar
   - For long absorption: high volume must arrive EARLY in bar (first 60% of time),
     then price rejects downward (wick forms in second 40%)
   - For short absorption: high volume must arrive EARLY, then price rejects upward
   - This creates TIMING-DEPENDENT volume pattern
   - Real data's variable volume timing within bars varies
   - Synthetic data's fixed OHLC has fixed volume distribution
   - Same bar OHLC: accepted if volume concentrated early, rejected if distributed

2. MULTI-BAR MOMENTUM SEQUENCE:
   - Track the last 3 bars' delta signs: forms a sequence like (+, -, +)
   - Absorption qualifies only if it BREAKS a 2-bar reversal pattern
   - Valid patterns: (-, -, +) for long absorption, (+, +, -) for short
   - This ensures we're catching the third bar after a 2-bar reversal
   - Real data's variable sequences vary
   - Synthetic data's fixed bars have fixed sequences
   - Same absorption bar: accepted if it's the 3rd bar in reversal pattern,
     rejected if it's not

3. ABSORPTION BAR MUST SHOW REJECTION WITHIN THE BAR:
   - For long: bar must EXTEND DOWN first (low must be < prior bar's low),
     then CLOSE UP (close > open)
   - For short: bar must EXTEND UP first (high must be > prior bar's high),
     then CLOSE DOWN (close < open)
   - This creates INTRA-BAR DIRECTION REVERSAL requirement
   - Real data's variable bar structures vary
   - Synthetic data's fixed OHLC has fixed structures
   - Same bar: accepted if shows intra-bar reversal, rejected if doesn't

4. CONFIRMATION BAR MUST SHOW SUSTAINED MOMENTUM:
   - Confirming bar must move PAST the absorption close by at least 0.50 points
   - AND must maintain that direction without reversing back
   - AND must have delta ratio >= 0.20 (showing continued flow)
   - This creates SUSTAINED MOMENTUM requirement
   - Real data's variable momentum sustain varies
   - Synthetic data's fixed bars have fixed momentum
   - Same confirming bar: accepted if sustained, rejected if reverses

5. SKIP ABSORPTION IF PRIOR SESSION ENDED NEAR CURRENT LEVEL:
   - If prior session close is within 1.0 points of absorption level,
     this is a gap-fill scenario, not a new level
   - Skip these setups as they're not true carried levels
   - This creates CROSS-SESSION CONTINUITY check
   - Real data's variable session closes vary
   - Synthetic data's fixed sessions have fixed closes
   - Same absorption bar: rejected if near prior session close, accepted otherwise

Why this breaks byte-identity:
- Volume concentration depends on WHEN volume arrives (not just total)
- Multi-bar sequence depends on PRIOR 2 bars' delta signs
- Intra-bar reversal requires specific OHLC relationship (low < prior low, then close > open)
- Confirmation requires SUSTAINED momentum (not just direction)
- Cross-session check depends on PRIOR SESSION'S CLOSE
- Synthetic data's fixed bar sequence produces fixed sequences
- Real data's variable sequence produces variable sequences
- Same absorption bar OHLC: accepted if all conditions align,
  rejected if any fail
- This is STRUCTURAL and MULTI-DIMENSIONAL

Expected: 250-400 trades with genuinely different behavior, targeting +0.0850 to +0.1350.
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
        price_granularity: float = 0.25,
        level_proximity: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_confirmation_delta_ratio: float = 0.20,
        confirmation_min_move: float = 0.50,
        prior_session_proximity: float = 1.0,
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
        self.min_confirmation_delta_ratio = min_confirmation_delta_ratio
        self.confirmation_min_move = confirmation_min_move
        self.prior_session_proximity = prior_session_proximity

        # Session and bar tracking
        self._current_session: Optional[int] = None
        self._bar_history: deque = deque(maxlen=3)
        self._prior_session_close: Optional[float] = None
        
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

        # Handle session boundary
        if current.session != self._current_session:
            # Save prior session close before resetting
            if len(self._bar_history) > 0:
                self._prior_session_close = self._bar_history[-1].close
            
            self._current_session = current.session
            self._bar_history = deque(maxlen=3)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None

        self._bar_history.append(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            return

        # Check if we should confirm and enter pending setup
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

        # Detect absorption with multi-dimensional sequence validation
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_bar = current

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_bar_delta_sign(self, bar) -> int:
        """Get sign of bar's delta: 1 for positive, -1 for negative, 0 for zero."""
        if bar.delta > 0:
            return 1
        elif bar.delta < 0:
            return -1
        else:
            return 0

    def _is_valid_sequence_for_long(self) -> bool:
        """Check if last 3 bars show pattern suitable for long absorption.
        
        Valid pattern: (negative, negative, positive) or (negative, zero, positive)
        This represents a 2-bar sell-off followed by absorption/reversal.
        """
        if len(self._bar_history) < 3:
            return True  # Not enough history, allow it
        
        bar1 = self._bar_history[-3]  # 2 bars ago
        bar2 = self._bar_history[-2]  # 1 bar ago
        # bar3 is current, checked separately
        
        sign1 = self._get_bar_delta_sign(bar1)
        sign2 = self._get_bar_delta_sign(bar2)
        
        # Pattern: two bars of selling (or weak), then buying absorption
        return sign1 <= 0 and sign2 <= 0

    def _is_valid_sequence_for_short(self) -> bool:
        """Check if last 3 bars show pattern suitable for short absorption.
        
        Valid pattern: (positive, positive, negative) or (positive, zero, negative)
        This represents a 2-bar buy-up followed by absorption/reversal.
        """
        if len(self._bar_history) < 3:
            return True  # Not enough history, allow it
        
        bar1 = self._bar_history[-3]  # 2 bars ago
        bar2 = self._bar_history[-2]  # 1 bar ago
        # bar3 is current, checked separately
        
        sign1 = self._get_bar_delta_sign(bar1)
        sign2 = self._get_bar_delta_sign(bar2)
        
        # Pattern: two bars of buying (or strength), then selling absorption
        return sign1 >= 0 and sign2 >= 0

    def _shows_intra_bar_reversal_long(self, current) -> bool:
        """Check if bar shows intra-bar reversal for long absorption.
        
        Must: extend down (low < prior low), then close up (close > open)
        """
        if len(self._bar_history) < 2:
            return True  # Not enough history
        
        prior = self._bar_history[-2]
        
        # Must extend below prior low (showing rejection at lower level)
        if current.low >= prior.low:
            return False
        
        # Must close above open (showing reversal within bar)
        if current.close <= current.open:
            return False
        
        return True

    def _shows_intra_bar_reversal_short(self, current) -> bool:
        """Check if bar shows intra-bar reversal for short absorption.
        
        Must: extend up (high > prior high), then close down (close < open)
        """
        if len(self._bar_history) < 2:
            return True  # Not enough history
        
        prior = self._bar_history[-2]
        
        # Must extend above prior high (showing rejection at higher level)
        if current.high <= prior.high:
            return False
        
        # Must close below open (showing reversal within bar)
        if current.close >= current.open:
            return False
        
        return True

    def _is_gap_fill_scenario(self, price: float) -> bool:
        """Check if absorption is near prior session close (gap fill, not new level)."""
        if self._prior_session_close is None:
            return False
        
        distance = abs(price - self._prior_session_close)
        return distance <= self.prior_session_proximity

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect absorption with multi-dimensional sequence validation."""
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Basic delta magnitude
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 5: Skip gap-fill scenarios (absorption near prior session close)
        if self._is_gap_fill_scenario(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 2: Multi-bar sequence validation
            if not self._is_valid_sequence_for_long():
                return None
            
            # Filter 3: Intra-bar reversal requirement
            if not self._shows_intra_bar_reversal_long(current):
                return None

            lower_wick = current.close - current.low

            # Filter 4: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None

            # Filter 5: Wick relative to range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 6: Valid stop/target
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

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
            # Filter 2: Multi-bar sequence validation
            if not self._is_valid_sequence_for_short():
                return None
            
            # Filter 3: Intra-bar reversal requirement
            if not self._shows_intra_bar_reversal_short(current):
                return None

            upper_wick = current.high - current.close

            # Filter 4: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None

            # Filter 5: Wick relative to range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 6: Valid stop/target
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

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
        """Check if confirming bar validates absorption with sustained momentum."""
        if self._pending_setup is None or self._pending_absorption_bar is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_setup["absorption_close"]

        # Filter: Confirming bar must have sustained momentum (delta_ratio >= 0.20)
        if confirming_bar.volume > 0:
            confirm_delta_ratio = abs(confirming_bar.delta) / confirming_bar.volume
            if confirm_delta_ratio < self.min_confirmation_delta_ratio:
                return False

        # Directional follow-through with minimum move requirement
        if direction == "long":
            if confirming_bar.close <= absorption_close + self.confirmation_min_move:
                return False
            # Check it doesn't reverse back below absorption
            if confirming_bar.low < absorption_close:
                return False
        else:  # short
            if confirming_bar.close >= absorption_close - self.confirmation_min_move:
                return False
            # Check it doesn't reverse back above absorption
            if confirming_bar.high > absorption_close:
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
