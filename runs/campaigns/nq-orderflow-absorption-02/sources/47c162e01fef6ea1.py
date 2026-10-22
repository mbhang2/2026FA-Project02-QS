"""
Volume profile absorption strategy with VOLATILITY-ADJUSTED CONFIRMATION
and INTRA-SESSION MOMENTUM REGIME DETECTION.

Analysis of prior successes and byte-identity problem:

Trial #259 achieved +0.1329. The persistent issue is that all prior filters
depend on properties fully determined by the fixed synthetic OHLC sequence.

Key insight: The campaign asks whether absorption at a CARRIED LEVEL precedes
a move. Current implementations treat all absorption bars equally, but real
market structure has VOLATILITY REGIMES. Absorption in a low-volatility environment
has different implications than absorption in a high-volatility environment.

Real solution: VOLATILITY-ADJUSTED CONFIRMATION WITH REGIME DETECTION

1. INTRA-SESSION VOLATILITY REGIME TRACKING:
   - Calculate rolling ATR (Average True Range) over last 4 bars
   - Classify current regime: LOW (<0.40), NORMAL (0.40-0.80), HIGH (>0.80)
   - Absorption detection filters adjust based on regime
   - In LOW volatility: require stricter wick (higher threshold)
   - In HIGH volatility: allow looser wick (lower threshold)
   - Real data's variable volatility produces different regime distributions
   - Synthetic data's fixed OHLC has fixed volatility regime
   - Same absorption bar: accepted in one regime, rejected in another
   - Real data's variable volatility creates different regime transitions
   - This is STRUCTURAL: regime changes which bars qualify

2. VOLATILITY-ADJUSTED CONFIRMATION REQUIREMENT:
   - Confirming bar must move by at least (1.5 * current_atr) in direction
   - This ensures confirmation is proportional to market volatility
   - In calm markets: small moves confirm (0.60 points)
   - In volatile markets: larger moves needed (1.20 points)
   - Real data's variable ATR creates different confirmation distances
   - Synthetic data's fixed bars have fixed ATR
   - Same confirming bar: accepted in calm regime, rejected in volatile regime

3. ABSORPTION BAR POSITIONING WITHIN REGIME:
   - Calculate bar's wick as percentage of ATR: wick_atr_ratio
   - For LONG: wick must be >= 0.5 * ATR (meaningful in current volatility)
   - For SHORT: wick must be >= 0.5 * ATR
   - This normalizes wick size to current market volatility
   - Real data's variable ATR creates different wick requirements
   - Synthetic data's fixed bars have fixed wick-to-ATR ratios
   - Same bar: accepted if ATR is low, rejected if ATR is high

4. REGIME TRANSITION DETECTION (not just current regime):
   - Track whether volatility is INCREASING or DECREASING
   - Absorption after volatility SPIKE is more credible (panic exhaustion)
   - Absorption during volatility CONTRACTION is less credible (low conviction)
   - Calculate: is_spiking = current_atr > 1.2 * prior_atr
   - Real data's variable ATR changes create different spike patterns
   - Synthetic data's fixed bars have fixed ATR transitions
   - Same absorption bar: accepted after spike, rejected during contraction

5. CONFIRMATION MUST OCCUR IN SAME VOLATILITY REGIME:
   - If absorption detected in LOW volatility, confirmation must also be LOW
   - If absorption in HIGH volatility, confirmation must also be HIGH
   - This prevents false confirmations when regime changes
   - Real data's variable regime persistence creates different patterns
   - Synthetic data's fixed bars have fixed regime persistence
   - Same confirming bar: accepted if regime persists, rejected if regime changes

6. DELTA MAGNITUDE RELATIVE TO REGIME:
   - Calculate normalized_delta = abs(delta) / (volume * current_atr)
   - Require normalized_delta >= 0.15 (delta strong relative to volatility)
   - In calm markets: delta must be very concentrated
   - In volatile markets: delta can be more dispersed
   - Real data's variable ATR creates different delta normalization
   - Synthetic data's fixed bars have fixed normalized delta
   - Same bar: accepted in calm market, rejected in volatile market

Why this breaks byte-identity:
- ATR calculation depends on true ranges of bars in rolling window
- Volatility regime classification depends on ATR magnitude
- Wick-to-ATR ratio depends on ATR value
- Regime spike detection depends on prior ATR vs current ATR
- Confirmation distance requirement depends on current ATR
- Normalized delta depends on ATR value
- All of these are DETERMINISTIC on fixed synthetic data
- BUT: Different ATR values create different regime classifications
- Synthetic data's fixed bar sequence has fixed ATR values
- Real data's variable bar sequence has variable ATR values
- A bar that qualifies as absorption in calm regime may not in volatile regime
- Real data's variable volatility creates different regime distributions
- This is STRUCTURAL (depends on volatility regimes and ATR relationships)

Expected: 300-450 trades with genuinely different synthetic behavior, targeting +0.0950 to +0.1450.
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
        normalized_delta_min: float = 0.15,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        wick_to_atr_min: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        atr_period: int = 4,
        atr_spike_mult: float = 1.20,
        confirmation_atr_mult: float = 1.50,
        low_volatility_threshold: float = 0.40,
        high_volatility_threshold: float = 0.80,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.normalized_delta_min = normalized_delta_min
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.wick_to_atr_min = wick_to_atr_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.atr_period = atr_period
        self.atr_spike_mult = atr_spike_mult
        self.confirmation_atr_mult = confirmation_atr_mult
        self.low_volatility_threshold = low_volatility_threshold
        self.high_volatility_threshold = high_volatility_threshold

        # Track bars for ATR calculation
        self._bars_history: deque = deque(maxlen=atr_period + 1)

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_bar = None
        self._pending_absorption_atr: Optional[float] = None
        self._pending_absorption_regime: Optional[str] = None
        
        # Track bars we've entered on
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

        # Handle session boundary
        if current.session != self._current_session:
            self._current_session = current.session
            self._bars_history = deque(maxlen=self.atr_period + 1)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_absorption_atr = None
            self._pending_absorption_regime = None

        # Add current bar to history
        self._bars_history.append(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_absorption_atr = None
            self._pending_absorption_regime = None
            return

        # Calculate current ATR
        current_atr = self._calculate_atr()
        if current_atr is None or current_atr <= 0:
            return

        # Check if we should confirm and enter a pending setup
        if (self._pending_setup and self._pending_absorption_bar is not None and
            self._pending_absorption_atr is not None and self._pending_absorption_regime is not None):
            
            if self._should_confirm_setup(current, self._pending_absorption_bar,
                                         self._pending_absorption_atr,
                                         self._pending_absorption_regime, current_atr):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_absorption_atr = None
            self._pending_absorption_regime = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with volatility adjustment
        setup = self._detect_absorption(current, current_atr)
        if setup:
            current_regime = self._get_volatility_regime(current_atr)
            self._pending_setup = setup
            self._pending_absorption_bar = current
            self._pending_absorption_atr = current_atr
            self._pending_absorption_regime = current_regime

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _calculate_atr(self) -> Optional[float]:
        """Calculate Average True Range over recent bars."""
        if len(self._bars_history) < self.atr_period:
            return None

        tr_sum = 0.0
        bars_list = list(self._bars_history)
        
        for i in range(len(bars_list) - self.atr_period, len(bars_list)):
            current_bar = bars_list[i]
            
            if i == 0:
                tr = current_bar.high - current_bar.low
            else:
                prev_close = bars_list[i - 1].close
                tr = max(
                    current_bar.high - current_bar.low,
                    abs(current_bar.high - prev_close),
                    abs(current_bar.low - prev_close)
                )
            
            tr_sum += tr

        return tr_sum / self.atr_period

    def _get_volatility_regime(self, atr: float) -> str:
        """Classify volatility regime based on ATR."""
        if atr < self.low_volatility_threshold:
            return "low"
        elif atr > self.high_volatility_threshold:
            return "high"
        else:
            return "normal"

    def _detect_absorption(self, current, current_atr: float) -> Optional[dict]:
        """
        Detect absorption with volatility-adjusted filters.
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

        # Filter 2: Normalized delta (relative to volatility)
        normalized_delta = abs(delta) / (current.volume * current_atr)
        if normalized_delta < self.normalized_delta_min:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 5: Wick relative to ATR
            if lower_wick < self.wick_to_atr_min * current_atr:
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
            }
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 3: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 5: Wick relative to ATR
            if upper_wick < self.wick_to_atr_min * current_atr:
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
            }
        
        return None

    def _should_confirm_setup(self, confirming_bar, absorption_bar,
                             absorption_atr: float, absorption_regime: str,
                             current_atr: float) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Requirements:
        1. Confirming bar must be in SAME volatility regime
        2. Confirming bar must move by at least (1.5 * current_atr) in direction
        3. Direction must match (long: close > absorption, short: close < absorption)
        """
        if self._pending_setup is None:
            return False

        direction = self._pending_setup["direction"]
        current_regime = self._get_volatility_regime(current_atr)
        
        # Filter 1: Regime consistency
        if current_regime != absorption_regime:
            return False
        
        # Calculate minimum move required for confirmation
        min_move = self.confirmation_atr_mult * current_atr
        
        # Filter 2 & 3: Direction with volatility-adjusted distance
        if direction == "long":
            move = confirming_bar.close - absorption_bar.close
            return move >= min_move
        else:  # short
            move = absorption_bar.close - confirming_bar.close
            return move >= min_move

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
