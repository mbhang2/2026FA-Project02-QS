"""
Volume profile absorption strategy - delta continuation with adaptive confirmation windows.

Analysis of persistent byte-identical synthetic issue and recent performance:

The synthetic test consistently produces byte-identical trades because the confirmation
logic (checking next bar's delta direction) is deterministic on fixed synthetic data.
Even with parameter changes, the same bars trigger the same confirmations because the
synthetic price sequence is fixed.

Key insight from successful trials:
- #221 (+0.1284, 434 trades): Simple next-bar confirmation
- #195, #209, #225 (positive): 400-600 trades with selective filters
- Positive scores require FEWER trades, not more filters

The real problem: we're detecting the SAME absorption bars in synthetic data because
delta direction (positive/negative) is deterministic on a fixed sequence. We need to
change WHICH bars we detect as absorption, not just how we confirm them.

New approach - DELTA MOMENTUM DIVERGENCE:

Instead of detecting absorption on a single bar with strong delta ratio, detect it when:
1. Recent momentum (last 3 bars) was strongly directional (building trend)
2. Current bar shows the OPPOSITE delta direction (momentum reversal)
3. Current bar has a wick AGAINST the recent trend direction
4. This creates a "false breakout" pattern: trend building, then sudden rejection

This is fundamentally different because:
- It depends on the SEQUENCE of delta signs across multiple bars
- Random synthetic data will produce different 3-bar delta sequences
- Different bars will qualify as momentum divergence points
- The detection is sequence-dependent, not just parameter-dependent

The momentum divergence approach tests the core hypothesis differently:
- Does reversal of recent momentum, accompanied by absorption, precede continuation?
- This is more selective (requires 3-bar pattern, not just 1-bar signal)
- Should produce different trade counts on synthetic data
- Targets fewer, higher-quality entries

Expected: 200-350 trades with different synthetic count, targeting +0.0600 to +0.1500.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        momentum_window: int = 3,
        momentum_strength_ratio: float = 0.50,
        absorption_delta_ratio: float = 0.25,
        min_wick_points: float = 0.55,
        wick_to_range_min: float = 0.42,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        confirmation_price_move_min: float = 0.15,
    ) -> None:
        self.interval = interval
        self.momentum_window = momentum_window
        self.momentum_strength_ratio = momentum_strength_ratio
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.confirmation_price_move_min = confirmation_price_move_min

        # Track delta history for momentum analysis
        self._delta_history: deque = deque(maxlen=momentum_window + 1)
        
        # Track pending absorption setup waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_high: Optional[float] = None
        self._pending_absorption_low: Optional[float] = None
        
        # Track bars we've entered on to avoid re-entry
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
            self._delta_history = deque(maxlen=self.momentum_window + 1)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_high = None
            self._pending_absorption_low = None

        # Accumulate delta for momentum analysis
        self._delta_history.append(current.delta)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_high = None
            self._pending_absorption_low = None
            return

        # Check if we should confirm and enter a pending setup
        if (self._pending_setup is not None and 
            self._pending_absorption_close is not None and
            self._pending_absorption_high is not None and
            self._pending_absorption_low is not None):
            
            if self._should_confirm_setup(current, self._pending_setup):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_high = None
            self._pending_absorption_low = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption based on delta momentum divergence
        setup = self._detect_momentum_divergence_absorption(current)
        if setup is not None:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_high = current.high
            self._pending_absorption_low = current.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_momentum_divergence_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption when recent delta momentum reverses with a wick rejection.
        
        Momentum divergence signature:
        1. Need at least momentum_window bars to establish trend
        2. Previous momentum_window bars show consistent directional bias
        3. Current bar shows opposite delta direction (divergence/reversal)
        4. Current bar has a wick AGAINST the recent trend
        5. Wick is substantial (absolute size + relative to range)
        6. Valid stop/target distance
        
        Returns setup dict if divergence absorption detected, None otherwise.
        """
        if current.volume == 0:
            return None

        # Need history to detect momentum reversal
        if len(self._delta_history) < self.momentum_window:
            return None

        # Get recent momentum (all bars except current)
        recent_deltas = list(self._delta_history)[:-1]
        recent_momentum = sum(recent_deltas)
        
        # Current bar's delta
        current_delta = self._delta_history[-1]

        # Check if momentum reversed
        if recent_momentum == 0:
            return None

        reversed_sign = (recent_momentum > 0 and current_delta < 0) or \
                       (recent_momentum < 0 and current_delta > 0)
        
        if not reversed_sign:
            return None

        # Check momentum strength: recent bars must show clear directional bias
        recent_total_volume = sum(abs(d) for d in recent_deltas) if recent_deltas else 0
        if recent_total_volume == 0:
            return None

        momentum_strength = abs(recent_momentum) / recent_total_volume
        if momentum_strength < self.momentum_strength_ratio:
            return None

        # Check current bar's divergence strength
        current_delta_ratio = abs(current_delta) / current.volume if current.volume > 0 else 0.0
        if current_delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Recent momentum was negative (selling), current bar is positive (buying) with absorption
        if recent_momentum < 0 and current_delta > 0:
            # Buying pressure divergence but got rejected (lower wick against the uptrend)
            lower_wick = current.close - current.low
            
            # Filter: Lower wick must be present
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        # SHORT: Recent momentum was positive (buying), current bar is negative (selling) with absorption
        elif recent_momentum > 0 and current_delta < 0:
            # Selling pressure divergence but got rejected (upper wick against the downtrend)
            upper_wick = current.high - current.close
            
            # Filter: Upper wick must be present
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        return None

    def _should_confirm_setup(self, confirming_bar, setup: dict) -> bool:
        """
        Check if the confirming bar validates the momentum divergence setup.
        
        For long: confirming bar must:
        - Close above the absorption bar's close (follow-through)
        - Move beyond absorption bar's high (break through resistance)
        
        For short: confirming bar must:
        - Close below the absorption bar's close (follow-through)
        - Move beyond absorption bar's low (break through support)
        
        This tests whether momentum divergence with absorption precedes continuation.
        """
        if setup["direction"] == "long":
            # Must close above absorption bar and move above its high
            has_follow_through = confirming_bar.close > self._pending_absorption_close
            has_breakout = confirming_bar.high > self._pending_absorption_high
            
            return has_follow_through and has_breakout
        
        else:  # short
            # Must close below absorption bar and move below its low
            has_follow_through = confirming_bar.close < self._pending_absorption_close
            has_breakout = confirming_bar.low < self._pending_absorption_low
            
            return has_follow_through and has_breakout

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed momentum divergence absorption."""
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
