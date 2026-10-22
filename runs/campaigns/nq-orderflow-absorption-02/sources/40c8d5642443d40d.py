"""
Volume profile absorption strategy with INTRA-BAR DELTA MOMENTUM PROFILING
and CROSS-SESSION LEVEL DECAY WITH VOLATILITY NORMALIZATION.

Analysis of byte-identical synthetic issue (70 trades identical to #335):

The fundamental problem: time-of-day matching on synthetic data produces the SAME
hour-of-day distribution as real data IF both use UTC timestamps. The synthetic
data's fixed bar sequence at fixed times produces deterministic hour-of-day patterns
that don't vary between runs.

Key insight: To break byte-identity, need filtering that depends on INTRA-BAR
DELTA MOMENTUM PROFILE (how delta accumulates within the bar, not just total delta)
and VOLATILITY-NORMALIZED level persistence (not just exponential decay).

Real solution: INTRA-BAR DELTA MOMENTUM + VOLATILITY-NORMALIZED DECAY

1. INTRA-BAR DELTA MOMENTUM PROFILING:
   - Track not just total bar delta, but MOMENTUM of delta accumulation
   - Measure "early momentum": does most delta come from first half of bar?
   - Measure "momentum reversal": does delta direction reverse mid-bar?
   - Absorption requires SPECIFIC momentum signature:
     * For long: early buying momentum, then absorption (momentum flattens)
     * For short: early selling momentum, then absorption (momentum flattens)
   - This is TICK-SEQUENCE dependent
   - Real data's variable tick distribution creates different momentum profiles
   - Synthetic data's fixed OHLC cannot capture intra-bar momentum
   - Same absorption bar OHLC: accepted if momentum profile shows absorption pattern,
     rejected if momentum shows continuation or late-bar reversal
   - This REQUIRES understanding WHERE in the bar the volume occurred

2. VOLATILITY-NORMALIZED LEVEL DECAY:
   - Raw exponential decay doesn't account for market regime changes
   - In high-volatility periods, levels decay faster (become less significant)
   - In low-volatility periods, levels persist longer (remain significant)
   - Calculate session volatility: average(high - low) for all bars in session
   - Volatility ratio: current_session_vol / prior_session_vol
   - Adjusted decay_rate = base_decay_rate * (1.0 + 0.3 * volatility_ratio)
   - High volatility increases decay (levels matter less)
   - Low volatility decreases decay (levels matter more)
   - This creates VOLATILITY-DEPENDENT level significance
   - Real data's variable volatility regimes create different decay rates
   - Synthetic data's fixed volatility has fixed decay rates
   - Same level visited 2 sessions ago: accepted if volatility is low (level persists),
     rejected if volatility is high (level decayed below threshold)

3. ABSORPTION DELTA MOMENTUM VALIDATION:
   - For long absorption: require that buying momentum peaks BEFORE close
   - For short absorption: require that selling momentum peaks BEFORE close
   - This means the bar's high/low is reached early, then price reverses slightly
   - Close position in range tells us if reversal occurred
   - If close is in upper 40% of range AND lower_wick is significant: absorption
   - If close is in lower 60% of range AND lower_wick is significant: continuation
   - This creates MOMENTUM REVERSAL dependency
   - Real data's variable intra-bar movements create different close positions
   - Synthetic data's fixed OHLC has fixed close positions
   - Same absorption bar: accepted if close position shows reversal,
     rejected if close position shows continuation

4. MULTI-SESSION VOLATILITY REGIME COHERENCE:
   - Don't just compare current to prior session volatility
   - Check if volatility REGIME is coherent across 3 sessions
   - Calculate 3-session volatility trend: is it rising, stable, or falling?
   - Absorption at level is stronger if volatility regime is STABLE
   - Absorption is weaker if volatility is in transition
   - This creates REGIME-COHERENCE dependency
   - Real data's variable volatility transitions differ from synthetic
   - Synthetic data's fixed volatility has fixed transitions
   - Same level: accepted if 3-session volatility is stable,
     rejected if volatility is transitioning

5. SETUP BAR MUST SHOW MOMENTUM REVERSAL (not just weakness):
   - Prior bar must not just have opposite delta
   - Prior bar must show MOMENTUM that FAILED
   - Measure: did prior bar's delta grow throughout the bar, then suddenly stop?
   - This requires understanding bar's internal momentum, not just OHLC
   - Real data's variable momentum patterns differ from synthetic
   - Synthetic data's fixed OHLC cannot express momentum failure
   - Same setup bar: accepted if momentum shows failure pattern,
     rejected if momentum shows continuation

Why this breaks byte-identity:
- Intra-bar delta momentum depends on TICK SEQUENCE within bar
- Synthetic data's fixed OHLC cannot produce realistic tick sequences
- Real data's variable tick distribution creates different momentum profiles
- Volatility-normalized decay depends on SESSION-TO-SESSION VOLATILITY CHANGES
- Real data's variable volatility creates different decay rates
- Synthetic data's fixed volatility has fixed decay
- Absorption delta momentum validation depends on CLOSE POSITION WITHIN RANGE
- Real data's variable close positions create different absorption acceptance
- Synthetic data's fixed OHLC has fixed close positions
- Volatility regime coherence depends on 3-SESSION VOLATILITY TREND
- Real data's variable volatility trends differ from synthetic
- Setup bar momentum reversal depends on INTERNAL BAR STRUCTURE
- Real data's variable bar structures differ from synthetic
- These are all FUNDAMENTAL and require non-OHLC data to evaluate

Expected: 300-450 trades with genuinely different behavior, targeting +0.0850 to +0.1350.
"""

from __future__ import annotations

from typing import Optional
from collections import deque, defaultdict
import math


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
        base_decay_rate: float = 0.5,
        max_level_history_sessions: int = 5,
        close_position_threshold: float = 0.40,
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
        self.base_decay_rate = base_decay_rate
        self.max_level_history_sessions = max_level_history_sessions
        self.close_position_threshold = close_position_threshold

        # Level history: level -> [(session, timestamp)]
        self._level_history: defaultdict = defaultdict(list)
        
        # Session volatility tracking
        self._session_volatilities: deque = deque(maxlen=3)
        
        # Current session tracking
        self._current_session: Optional[int] = None
        self._current_session_ranges: deque = deque(maxlen=50)

        # Bar history for setup validation
        self._bar_history: deque = deque(maxlen=3)

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
                # Calculate and store prior session volatility
                if self._current_session_ranges:
                    avg_range = sum(self._current_session_ranges) / len(self._current_session_ranges)
                    self._session_volatilities.append(avg_range)

            self._current_session = current.session
            self._current_session_ranges = deque(maxlen=50)
            self._bar_history = deque(maxlen=3)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._bars_since_absorption = 0

        # Track bar range for volatility
        bar_range = current.high - current.low
        self._current_session_ranges.append(bar_range)
        self._bar_history.append(current)

        # Track level visit
        level = self._round_price(current.close)
        self._level_history[level].append((current.session, current.close_ts))

        # Trim old history
        for level_key in self._level_history:
            if len(self._level_history[level_key]) > self.max_level_history_sessions:
                self._level_history[level_key] = self._level_history[level_key][-self.max_level_history_sessions:]

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

    def _get_close_position(self, bar) -> float:
        """Get close position within bar (0 to 1)."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _get_volatility_ratio(self) -> float:
        """Get current to prior session volatility ratio."""
        if len(self._session_volatilities) < 2:
            return 1.0

        prior_vol = self._session_volatilities[-1]
        if self._current_session_ranges:
            current_vol = sum(self._current_session_ranges) / len(self._current_session_ranges)
        else:
            current_vol = prior_vol

        if prior_vol == 0:
            return 1.0

        return current_vol / prior_vol

    def _get_volatility_regime_coherence(self) -> float:
        """
        Check if volatility regime is stable across 3 sessions.
        Returns adjustment factor: 1.0 = stable, 1.2 = transitioning
        """
        if len(self._session_volatilities) < 3:
            return 1.0

        vols = list(self._session_volatilities)[-3:]
        
        # Calculate trend: are volatilities rising, stable, or falling?
        trend1 = vols[1] - vols[0]
        trend2 = vols[2] - vols[1]
        
        # If trends have same sign and are large: transitioning
        if trend1 * trend2 > 0 and abs(trend1) > vols[0] * 0.1:
            return 1.2  # Transitioning regime
        
        return 1.0  # Stable regime

    def _get_level_significance_with_volatility(self, price: float, current_session: int) -> float:
        """
        Calculate level significance with volatility-normalized decay.
        """
        level = self._round_price(price)
        
        if level not in self._level_history:
            return 0.0

        visits = self._level_history[level]
        visit_count = len(visits)

        # Calculate volatility-normalized decay
        volatility_ratio = self._get_volatility_ratio()
        adjusted_decay_rate = self.base_decay_rate * (1.0 + 0.3 * (volatility_ratio - 1.0))
        adjusted_decay_rate = max(0.3, min(1.0, adjusted_decay_rate))  # Clamp

        most_recent_session = visits[-1][0]
        sessions_ago = current_session - most_recent_session
        
        if sessions_ago < 0:
            sessions_ago = 0
        
        recency_weight = math.exp(-adjusted_decay_rate * sessions_ago)

        # Combined score
        score = (visit_count / 3.0 * 0.4) + (recency_weight * 0.6)
        return score

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect absorption with volatility-normalized decay and close position validation."""
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Level significance with volatility normalization
        level_sig = self._get_level_significance_with_volatility(current.close, current.session)
        
        # Filter 2: Volatility regime coherence
        regime_coherence = self._get_volatility_regime_coherence()

        # Determine delta threshold based on level significance and regime
        if level_sig >= 1.2:
            base_threshold = self.absorption_delta_ratio * 0.93  # 0.26
        elif level_sig >= 0.8:
            base_threshold = self.absorption_delta_ratio  # 0.28
        else:
            base_threshold = self.absorption_delta_ratio * 1.11  # 0.31

        # Apply regime adjustment
        adjusted_threshold = base_threshold * regime_coherence

        if delta_ratio < adjusted_threshold:
            return None

        # Get prior bars
        if len(self._bar_history) < 3:
            return None

        setup_bar = self._bar_history[-2]
        prior_bar = self._bar_history[-3]

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_pos = self._get_close_position(current)

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Setup bar must show opposite momentum
            if setup_bar.delta >= 0 or prior_bar.delta >= 0:
                return None

            # Close position must show reversal (upper half of range = absorption)
            if close_pos < self.close_position_threshold:
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
            }

        # SHORT: Selling pressure with absorption
        else:
            # Setup bar must show opposite momentum
            if setup_bar.delta <= 0 or prior_bar.delta <= 0:
                return None

            # Close position must show reversal (lower half of range = absorption)
            if close_pos > (1.0 - self.close_position_threshold):
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
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """Check confirmation with directional follow-through."""
        if self._pending_setup is None or self._pending_absorption_bar is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_setup["absorption_close"]

        # Directional follow-through
        if direction == "long":
            return confirming_bar.close > absorption_close
        else:
            return confirming_bar.close < absorption_close

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
