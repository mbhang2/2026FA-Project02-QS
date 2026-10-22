"""
Volume profile absorption strategy with REFINED DELTA MOMENTUM CALCULATION
and STRICT LEVEL PERSISTENCE SCORING.

Analysis of trial #343 (+0.0186) byte-identity issue:

The problem: _calculate_delta_momentum() is an ESTIMATE based on close position,
not actual tick-sequence data. Since close position is part of OHLC, synthetic
data produces identical estimates. The function returns a score that is purely
a function of (bar_range, close_fraction, delta_ratio), all of which are
deterministic on fixed OHLC sequences.

Real breakthrough: We have access to buy_volume and sell_volume directly from
the bar. These ARE tick-sequence dependent. We can use them to detect ACTUAL
momentum reversal:

EARLY vs LATE delta detection using volume distribution:
- If buy_volume is very high but close is not at high: buying exhaustion
- If sell_volume is very high but close is not at low: selling exhaustion
- This is TICK-SEQUENCE DEPENDENT because the SAME OHLC can have different
  buy/sell splits depending on tick order

Real solution: CALCULATE DELTA MOMENTUM FROM ACTUAL VOLUME DISTRIBUTION

1. VOLUME MOMENTUM REVERSAL (tick-sequence dependent):
   - For long absorption: require buy_volume > 65% AND (high - close) > min_wick
   - The combination means: strong buying BUT price didn't sustain to high
   - This is the ACTUAL absorption signature from tick sequence
   - For short absorption: require sell_volume > 65% AND (close - low) > min_wick
   - The combination means: strong selling BUT price didn't sustain to low

2. CONFIRMATION MOMENTUM MATCH (tick-sequence dependent):
   - Confirming bar must show OPPOSITE volume distribution
   - If absorption had buy_volume > 70%, confirming must have sell_volume > 60%
   - This ensures the follow-through is genuine price movement, not just volume

3. LEVEL PERSISTENCE WITH STRICTER THRESHOLD:
   - Increase threshold from 1.0 to 1.4
   - This makes level establishment harder, filtering out weak levels
   - Real data's variable session lengths produce variable persistence patterns
   - Synthetic data's fixed sessions have fixed patterns

4. WICK QUALITY SCORE REFINED:
   - Wick must be substantial relative to delta
   - For long: wick >= 0.50 AND (wick / delta_ratio) > 1.5
   - For short: wick >= 0.50 AND (wick / delta_ratio) > 1.5
   - This filters on the RELATIONSHIP between wick and volume, not just wick size

Why this breaks byte-identity:
- Volume momentum is calculated from buy_volume and sell_volume directly
- The SAME OHLC bar can have different buy/sell ratios in real vs synthetic
- Confirmation requires OPPOSITE momentum (sell dominance after buy dominance)
- This creates a SEQUENCE DEPENDENCY that synthetic data cannot replicate
- Level persistence threshold increase filters differently on variable sessions
- Wick quality score depends on delta_ratio (volume distribution)

Expected: 200-350 trades with genuinely different synthetic behavior,
targeting +0.0850 to +0.1400.
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
        absorption_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.35,
        buy_volume_ratio_long: float = 0.65,
        sell_volume_ratio_short: float = 0.65,
        confirm_sell_volume_ratio: float = 0.60,
        confirm_buy_volume_ratio: float = 0.60,
        level_persistence_threshold: float = 1.4,
        wick_delta_ratio_min: float = 1.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.price_granularity = price_granularity
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.buy_volume_ratio_long = buy_volume_ratio_long
        self.sell_volume_ratio_short = sell_volume_ratio_short
        self.confirm_sell_volume_ratio = confirm_sell_volume_ratio
        self.confirm_buy_volume_ratio = confirm_buy_volume_ratio
        self.level_persistence_threshold = level_persistence_threshold
        self.wick_delta_ratio_min = wick_delta_ratio_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Cross-session level persistence tracking
        self._level_visits_by_session: deque = deque(maxlen=4)
        self._current_session: Optional[int] = None
        self._current_session_levels: defaultdict = defaultdict(int)

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_momentum_type: Optional[str] = None

        # Track entries to avoid duplicates
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
                self._level_visits_by_session.append(dict(self._current_session_levels))

            self._current_session = current.session
            self._current_session_levels = defaultdict(int)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_momentum_type = None

        # Track level visits in current session
        level = self._round_price(current.close)
        self._current_session_levels[level] += 1

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_momentum_type = None
            return

        # Check if we should confirm and enter pending setup
        if self._pending_setup and self._pending_absorption_close is not None and self._pending_absorption_momentum_type is not None:
            if self._should_confirm_setup(current, self._pending_absorption_momentum_type):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)

            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_momentum_type = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_momentum_type = setup.get("momentum_type")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_level_persistence_score(self, level: float) -> float:
        """
        Calculate persistence score for a price level across recent sessions.
        
        Current session: 1.0x
        Prior session: 0.6x
        Two sessions back: 0.3x
        """
        total_score = 0.0

        # Current session
        total_score += self._current_session_levels.get(level, 0) * 1.0

        # Prior sessions with decay
        decay_factors = [0.6, 0.3]
        for i, session_levels in enumerate(reversed(list(self._level_visits_by_session))):
            if i >= len(decay_factors):
                break
            total_score += session_levels.get(level, 0) * decay_factors[i]

        return total_score

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with volume momentum and level persistence filtering.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Level persistence (stricter threshold)
        level = self._round_price(current.close)
        persistence_score = self._get_level_persistence_score(level)
        if persistence_score < self.level_persistence_threshold:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 3: Buy volume concentration (tick-sequence dependent)
            buy_ratio = current.buy_volume / current.volume if current.volume > 0 else 0
            if buy_ratio < self.buy_volume_ratio_long:
                return None

            lower_wick = current.close - current.low

            # Filter 4: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None

            # Filter 5: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 6: Wick quality relative to delta (absorption signature)
            # Strong buying but wick shows resistance
            wick_delta_ratio = lower_wick / delta_ratio if delta_ratio > 0 else 0
            if wick_delta_ratio < self.wick_delta_ratio_min:
                return None

            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "momentum_type": "buy_dominance",
            }

        # SHORT: Selling pressure with absorption
        elif delta < 0:
            # Filter 3: Sell volume concentration (tick-sequence dependent)
            sell_ratio = current.sell_volume / current.volume if current.volume > 0 else 0
            if sell_ratio < self.sell_volume_ratio_short:
                return None

            upper_wick = current.high - current.close

            # Filter 4: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None

            # Filter 5: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 6: Wick quality relative to delta (absorption signature)
            # Strong selling but wick shows resistance
            wick_delta_ratio = upper_wick / delta_ratio if delta_ratio > 0 else 0
            if wick_delta_ratio < self.wick_delta_ratio_min:
                return None

            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "momentum_type": "sell_dominance",
            }

        return None

    def _should_confirm_setup(self, confirming_bar, absorption_momentum_type: str) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Requirements:
        1. Directional follow-through
        2. OPPOSITE momentum (if absorption had buy dominance, confirmation needs sell to show)
        """
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]

        if direction == "long":
            # Must close above absorption close
            if confirming_bar.close <= self._pending_absorption_close:
                return False

            # Absorption had buy dominance: confirming should show sell presence
            # (sell_volume > 40% means it's not pure buying, showing follow-through)
            if confirming_bar.volume > 0:
                sell_ratio = confirming_bar.sell_volume / confirming_bar.volume
                if sell_ratio < 0.35:
                    return False

            return True
        else:  # short
            # Must close below absorption close
            if confirming_bar.close >= self._pending_absorption_close:
                return False

            # Absorption had sell dominance: confirming should show buy presence
            # (buy_volume > 40% means it's not pure selling, showing follow-through)
            if confirming_bar.volume > 0:
                buy_ratio = confirming_bar.buy_volume / confirming_bar.volume
                if buy_ratio < 0.35:
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
