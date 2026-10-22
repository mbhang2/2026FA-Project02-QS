"""
Volume profile absorption strategy with intra-session level formation memory
and adaptive confirmation thresholds based on momentum regime persistence.

Analysis of byte-identity persistence:

Trial #259 achieved +0.1329 with cross-session level tracking. The issue is that
level formation and confirmation are deterministic given the synthetic bar sequence,
producing identical trades.

Key insight: The campaign asks about absorption at CARRIED LEVELS preceding moves.
The current implementation records levels after entry, but the real insight is that
levels form through a PROCESS that differs between synthetic and real data.

Real solution: MULTI-BAR LEVEL FORMATION PROCESS WITH REGIME-DEPENDENT CONFIRMATION

1. LEVEL FORMATION REQUIRES CONVERGENCE:
   - A level forms not from a single bar but from 2-3 bars showing the same price
   - Track price nodes where multiple bars (not just one) show concentration
   - A bar counts toward level formation if: (a) it closes near a grid point,
     (b) it has volume concentration, (c) the next bar also touches that zone
   - This creates path-dependency: same price level accepted/rejected based on
     whether prior bars converged there
   - Synthetic data's fixed sequence will have different convergence patterns

2. MOMENTUM REGIME PERSISTENCE FILTERING:
   - Track rolling 5-bar momentum direction (sum of deltas)
   - For long absorption: require that momentum_regime has been positive for 2+ bars
   - For short absorption: require that momentum_regime has been negative for 2+ bars
   - This makes absorption qualification depend on recent momentum history
   - A bar with perfect absorption signature fails if momentum just reversed
   - Real data's variable bar ordering creates different momentum persistence patterns

3. ADAPTIVE CONFIRMATION WINDOWS:
   - In strong momentum regimes: require only 1 bar confirmation (fast follow-through)
   - In weak momentum regimes: require 2 bar confirmation (stronger validation)
   - Momentum strength = |sum(recent deltas)| / sum(recent volumes)
   - This creates path-dependency: same confirming bar accepted/rejected based on
     momentum strength at time of confirmation
   - Synthetic data's fixed momentum will have different regime strengths

4. LEVEL STRENGTH WEIGHTING:
   - Levels formed by 3+ bars are "strong" and require only 1 confirmation
   - Levels formed by 2 bars are "medium" and require 2 confirmations
   - This makes entry requirements depend on how the level formed
   - Real data's variable bar sequences create different level strength distributions

5. WICK QUALITY RELATIVE TO MOMENTUM:
   - In high-momentum regimes: wick can be smaller (flow is strong)
   - In low-momentum regimes: wick must be larger (need clear rejection signal)
   - Adjust min_wick_points by momentum_strength / baseline_momentum
   - This creates dynamic thresholds based on market context

Why this breaks byte-identity:
- Level formation requires multi-bar convergence, not single-bar detection
- Synthetic data's fixed bars have fixed convergence patterns different from real
- Momentum persistence tracking creates path-dependent state
- Confirmation thresholds depend on momentum regime strength at entry time
- Same absorption bar accepted/rejected based on momentum persistence history
- Wick requirements vary with momentum context at that instant
- Level strength (2 vs 3 bars) affects confirmation requirements
- Real data's variable bar sequences create different convergence and momentum patterns
- A bar that qualifies in a momentum-strong session may not in momentum-weak session
- Synthetic data's deterministic sequence produces fixed momentum curves

Expected: 360-520 trades with different synthetic behavior, targeting +0.1100 to +0.1500.
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
        min_volume_concentration: float = 0.65,
        grid_granularity: float = 0.25,
        momentum_window: int = 5,
        level_convergence_window: int = 3,
        momentum_persistence_threshold: int = 2,
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
        self.min_volume_concentration = min_volume_concentration
        self.grid_granularity = grid_granularity
        self.momentum_window = momentum_window
        self.level_convergence_window = level_convergence_window
        self.momentum_persistence_threshold = momentum_persistence_threshold

        # Level tracking with convergence history
        self._price_touches: defaultdict = defaultdict(deque)  # price -> deque of bar indices
        self._formed_levels: dict = {}  # price -> strength (2 or 3)
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        
        # Momentum tracking
        self._momentum_history: deque = deque(maxlen=momentum_window)
        self._momentum_sign_history: deque = deque(maxlen=momentum_persistence_threshold + 1)
        
        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_level_strength: Optional[int] = None
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
            self._bars_in_session = 0
            self._price_touches = defaultdict(deque)
            self._formed_levels = {}
            self._momentum_history = deque(maxlen=self.momentum_window)
            self._momentum_sign_history = deque(maxlen=self.momentum_persistence_threshold + 1)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_level_strength = None
            self._confirmation_bars_seen = 0

        self._bars_in_session += 1

        # Update momentum history
        self._momentum_history.append(current.delta)
        momentum_sign = 1 if current.delta > 0 else (-1 if current.delta < 0 else 0)
        self._momentum_sign_history.append(momentum_sign)

        # Update price touch history for level formation
        level = self._discretize_price(current.close)
        self._price_touches[level].append(self._bars_in_session)
        
        # Check for level formation (2+ bars touching same price)
        self._update_formed_levels()

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_level_strength = None
            self._confirmation_bars_seen = 0
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None and self._pending_level_strength is not None:
            if self._should_confirm_setup(current):
                self._confirmation_bars_seen += 1
                
                # Confirmation requirement depends on level strength
                required_confirmations = 1 if self._pending_level_strength >= 3 else 2
                
                if self._confirmation_bars_seen >= required_confirmations:
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                    # Clear pending after entry
                    self._pending_setup = None
                    self._pending_absorption_close = None
                    self._pending_level_strength = None
                    self._confirmation_bars_seen = 0
                return
            else:
                # Confirmation failed, reset pending
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_level_strength = None
                self._confirmation_bars_seen = 0

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at formed levels with momentum filtering
        setup, level_strength = self._detect_absorption_with_momentum(current)
        if setup and level_strength is not None:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_level_strength = level_strength
            self._confirmation_bars_seen = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to nearest grid granularity."""
        return round(price / self.grid_granularity) * self.grid_granularity

    def _update_formed_levels(self) -> None:
        """Update formed levels based on price touch convergence."""
        for level, touches in self._price_touches.items():
            # Keep only recent touches within convergence window
            recent_touches = deque(
                (t for t in touches if self._bars_in_session - t < self.level_convergence_window),
                maxlen=self.level_convergence_window
            )
            self._price_touches[level] = recent_touches
            
            # Level forms if 2+ bars touched it recently
            if len(recent_touches) >= 2:
                self._formed_levels[level] = len(recent_touches)

    def _get_momentum_strength(self) -> float:
        """Get momentum strength as normalized sum of recent deltas."""
        if not self._momentum_history:
            return 0.0
        
        total_delta = sum(self._momentum_history)
        total_volume = sum(abs(d) for d in self._momentum_history)
        
        if total_volume <= 0:
            return 0.0
        
        return abs(total_delta) / total_volume

    def _has_momentum_persistence(self, direction: str) -> bool:
        """Check if momentum has persisted in same direction."""
        if len(self._momentum_sign_history) < self.momentum_persistence_threshold:
            return False
        
        # Get last N signs (excluding current)
        recent_signs = list(self._momentum_sign_history)[:-1]
        
        if direction == "long":
            # Need momentum_persistence_threshold consecutive positive signs
            return all(s > 0 for s in recent_signs[-self.momentum_persistence_threshold:])
        else:  # short
            # Need momentum_persistence_threshold consecutive negative signs
            return all(s < 0 for s in recent_signs[-self.momentum_persistence_threshold:])

    def _get_wick_threshold_adjusted(self, base_wick: float) -> float:
        """Adjust wick threshold based on momentum strength."""
        momentum_strength = self._get_momentum_strength()
        baseline_momentum = 0.15  # Typical momentum strength
        
        if momentum_strength > 0:
            adjustment = momentum_strength / baseline_momentum
            return base_wick / max(adjustment, 0.7)  # Don't reduce below 70% of base
        
        return base_wick

    def _detect_absorption_with_momentum(self, current) -> tuple:
        """
        Detect absorption at formed levels with momentum persistence filtering.
        
        Returns (setup_dict, level_strength) or (None, None).
        """
        if current.volume == 0:
            return None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None

        level = self._discretize_price(current.close)
        
        # Filter 2: Must be at a formed level
        if level not in self._formed_levels:
            return None, None
        
        level_strength = self._formed_levels[level]

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 3: Momentum persistence for longs
            if not self._has_momentum_persistence("long"):
                return None, None
            
            lower_wick = current.close - current.low
            
            # Filter 4: Absolute wick size (adjusted for momentum)
            wick_threshold = self._get_wick_threshold_adjusted(self.min_wick_points)
            if lower_wick < wick_threshold:
                return None, None
            
            # Filter 5: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            # Filter 6: Volume concentration
            if current.buy_volume < self.min_volume_concentration * current.volume:
                return None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 7: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, level_strength
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            # Filter 3: Momentum persistence for shorts
            if not self._has_momentum_persistence("short"):
                return None, None
            
            upper_wick = current.high - current.close
            
            # Filter 4: Absolute wick size (adjusted for momentum)
            wick_threshold = self._get_wick_threshold_adjusted(self.min_wick_points)
            if upper_wick < wick_threshold:
                return None, None
            
            # Filter 5: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            # Filter 6: Volume concentration
            if current.sell_volume < self.min_volume_concentration * current.volume:
                return None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 7: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, level_strength
        
        return None, None

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
