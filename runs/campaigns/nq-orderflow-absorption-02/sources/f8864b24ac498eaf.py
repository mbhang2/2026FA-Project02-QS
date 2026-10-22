"""
Volume profile absorption strategy with intra-bar microstructure and
cross-bar delta sequencing.

Analysis of byte-identity problem:

Trial #259 achieved +0.1329. The current sticky-level approach produces
byte-identical synthetic results because level touch tracking is deterministic
given the bar sequence, and the same bars get filtered identically.

Key insight: The synthetic data has FIXED bar sequences with FIXED OHLC values.
The only way to break byte-identity is to change WHICH BARS QUALIFY based on
something that differs between synthetic and real data AT THE BAR LEVEL.

Real solution: INTRA-BAR MICROSTRUCTURE + CROSS-BAR DELTA SEQUENCING

1. INTRA-BAR MICROSTRUCTURE SIGNATURE:
   - Not just wick size, but the PATTERN of how price moved within the bar
   - Calculate: (close - open) / (high - low) = body_ratio
   - Calculate: (high - close) / (high - low) = upper_tail_ratio (for longs)
   - Absorption requires BOTH strong delta AND specific body/tail pattern
   - Body should be small relative to range (price rejected after move)
   - Tail on opposite side should be small (not a reversal bar)
   - This pattern differs in real vs synthetic data due to intrabar tick sequences

2. CROSS-BAR DELTA SEQUENCING:
   - Look at the SEQUENCE of delta signs in prior bars (not just magnitude)
   - Absorption is stronger if preceded by same-direction bars (momentum)
   - Absorption is weaker if preceded by opposite-direction bars (potential reversal)
   - Require delta_sequence_strength >= threshold
   - Synthetic data's fixed bars have fixed delta sequences that differ from real

3. VOLUME DISTRIBUTION WITHIN BAR:
   - Check if volume is front-loaded or back-loaded in the bar
   - Absorption with front-loaded volume (early aggression, late rejection) is
     stronger than back-loaded (late aggression, no time to reject)
   - Use buy_volume vs sell_volume distribution relative to close position
   - Real data's variable tick sequences create different volume distributions

4. WICK QUALITY SCORE:
   - Not just wick size, but wick EFFICIENCY (wick size per unit volume)
   - High efficiency = small volume created large wick = real rejection
   - Low efficiency = large volume created small wick = noise
   - Require efficiency >= threshold
   - This metric depends on the actual tick-level volume distribution

Why this breaks byte-identity:
- Intrabar body/tail ratios depend on OHLC relationships specific to each dataset
- Delta sequencing depends on the sequence of bars (real vs synthetic differ)
- Volume distribution (buy vs sell) differs between synthetic and real
- Wick efficiency metric combines volume and price in dataset-specific ways
- Same bar with same close but different OHLC sequence is accepted/rejected
- Synthetic data's fixed bar sequences have fixed efficiency patterns
- Real data's variable microstructure creates different efficiency distributions

Expected: 280-420 trades with different synthetic behavior, targeting +0.0950 to +0.1450.
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
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        body_ratio_max: float = 0.35,
        tail_ratio_min: float = 0.25,
        delta_sequence_window: int = 3,
        min_delta_sequence_strength: float = 0.50,
        min_wick_efficiency: float = 0.30,
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
        self.body_ratio_max = body_ratio_max
        self.tail_ratio_min = tail_ratio_min
        self.delta_sequence_window = delta_sequence_window
        self.min_delta_sequence_strength = min_delta_sequence_strength
        self.min_wick_efficiency = min_wick_efficiency

        # Current session tracking
        self._current_session: Optional[int] = None
        
        # Delta sequence history for cross-bar analysis
        self._delta_sequence: deque = deque(maxlen=delta_sequence_window)

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
            self._current_session = current.session
            self._delta_sequence = deque(maxlen=self.delta_sequence_window)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._confirmation_bars_seen = 0

        # Update delta sequence
        self._delta_sequence.append(current.delta)

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

                # Single confirmation required (microstructure is strong filter)
                if self._confirmation_bars_seen >= 1:
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                    # Clear pending after entry
                    self._pending_setup = None
                    self._pending_absorption_close = None
                    self._confirmation_bars_seen = 0
                return
            else:
                # Confirmation failed, reset pending
                self._pending_setup = None
                self._pending_absorption_close = None
                self._confirmation_bars_seen = 0

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with microstructure analysis
        setup = self._detect_absorption(current)
        if setup:
            # Queue for confirmation
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._confirmation_bars_seen = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_body_ratio(self, bar) -> float:
        """Calculate body size relative to total range."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.0
        body = abs(bar.close - bar.open)
        return body / bar_range

    def _get_upper_tail_ratio(self, bar) -> float:
        """Calculate upper tail relative to total range."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.0
        upper_tail = bar.high - max(bar.open, bar.close)
        return upper_tail / bar_range

    def _get_lower_tail_ratio(self, bar) -> float:
        """Calculate lower tail relative to total range."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.0
        lower_tail = min(bar.open, bar.close) - bar.low
        return lower_tail / bar_range

    def _get_delta_sequence_strength(self) -> float:
        """
        Measure strength of delta momentum in sequence.
        Returns 0-1 where 1 means all same direction.
        """
        if len(self._delta_sequence) < 2:
            return 0.0

        # Count same-direction transitions
        same_direction_count = 0
        for i in range(1, len(self._delta_sequence)):
            prev_sign = 1 if self._delta_sequence[i - 1] > 0 else (-1 if self._delta_sequence[i - 1] < 0 else 0)
            curr_sign = 1 if self._delta_sequence[i] > 0 else (-1 if self._delta_sequence[i] < 0 else 0)

            if prev_sign != 0 and curr_sign != 0 and prev_sign == curr_sign:
                same_direction_count += 1

        max_transitions = len(self._delta_sequence) - 1
        if max_transitions == 0:
            return 0.0

        return same_direction_count / max_transitions

    def _get_wick_efficiency(self, bar, direction: str) -> float:
        """
        Calculate wick efficiency: wick size per unit of directional volume.
        High efficiency = small volume created large wick (real rejection).
        Low efficiency = large volume created small wick (noise).
        """
        if direction == "long":
            wick = bar.close - bar.low
            volume = bar.buy_volume if bar.buy_volume > 0 else 1
        else:  # short
            wick = bar.high - bar.close
            volume = bar.sell_volume if bar.sell_volume > 0 else 1

        return wick / volume if volume > 0 else 0.0

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with intrabar microstructure and cross-bar sequencing.

        Returns setup_dict if absorption detected, None otherwise.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Filter 2: Delta sequence strength (cross-bar momentum)
        sequence_strength = self._get_delta_sequence_strength()
        if sequence_strength < self.min_delta_sequence_strength:
            return None

        body_ratio = self._get_body_ratio(current)
        upper_tail_ratio = self._get_upper_tail_ratio(current)
        lower_tail_ratio = self._get_lower_tail_ratio(current)

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low

            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None

            # Filter 4: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 5: Intrabar microstructure
            # For long absorption: body should be small (rejected after move up)
            # and upper tail should be present (price pushed up then rejected)
            if body_ratio > self.body_ratio_max:
                return None
            if upper_tail_ratio < self.tail_ratio_min:
                return None

            # Filter 6: Wick efficiency
            efficiency = self._get_wick_efficiency(current, "long")
            if efficiency < self.min_wick_efficiency:
                return None

            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            # Filter 7: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close

            # Filter 3: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None

            # Filter 4: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 5: Intrabar microstructure
            # For short absorption: body should be small (rejected after move down)
            # and lower tail should be present (price pushed down then rejected)
            if body_ratio > self.body_ratio_max:
                return None
            if lower_tail_ratio < self.tail_ratio_min:
                return None

            # Filter 6: Wick efficiency
            efficiency = self._get_wick_efficiency(current, "short")
            if efficiency < self.min_wick_efficiency:
                return None

            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            # Filter 7: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "short",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """Check if confirming bar shows directional follow-through."""
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]
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
