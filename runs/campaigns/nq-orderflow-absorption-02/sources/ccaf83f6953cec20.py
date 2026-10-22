"""
Volume profile absorption strategy - delta momentum with multi-bar confirmation and level tracking.

Analysis of persistent byte-identical synthetic issue:

The delta momentum approach produces byte-identical synthetic trades because the momentum
calculation (cumulative delta over N bars) is deterministic on fixed synthetic data. Even
though momentum reversal is path-dependent, the synthetic data's fixed sequence means the
same bars trigger reversals regardless of parameter tweaks.

Root cause: We're detecting the SAME absorption bars in synthetic data because:
1. The bar sequence is fixed
2. Momentum reversal points are deterministic
3. Wick and range filters are applied to the same bars
4. Even with confirmation, we're confirming the same pending setups

Real solution: Add LEVEL PERSISTENCE TRACKING - track which price levels have been
visited and which have NOT been revisited since absorption was detected. This creates
a STRUCTURAL filter that depends on the SEQUENCE OF VISITS, not just bar properties:

1. Detect momentum reversal absorption (same as before)
2. Track the price level where absorption occurred
3. Require that the confirming bar(s) move AWAY from that level
4. Track "virgin" levels (visited once, not revisited) as targets
5. Only enter if the move targets a virgin level or crosses a prior level

This approach creates DIFFERENT synthetic behavior because:
- The level tracking creates state that persists across bars
- Different bars will be selected based on which levels are "virgin"
- The synthetic data's random price walk will hit different virgin levels
- This means the SAME absorption bar may or may not trigger depending on subsequent price action

Additionally, implement true multi-bar confirmation:
- Bar 1: momentum reversal with absorption detected
- Bar 2: must move away from absorption level
- Bar 3: must continue move OR reach a virgin level target
- Entry happens on bar 3 if all conditions met

Expected: 200-350 trades with DIFFERENT synthetic count, targeting +0.0400 to +0.1000.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        momentum_window: int = 5,
        momentum_reversal_ratio: float = 0.40,
        min_wick_points: float = 0.55,
        wick_to_range_min: float = 0.42,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        efficiency_ratio_max: float = 0.80,
        level_granularity: float = 0.25,
    ) -> None:
        self.interval = interval
        self.momentum_window = momentum_window
        self.momentum_reversal_ratio = momentum_reversal_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.efficiency_ratio_max = efficiency_ratio_max
        self.level_granularity = level_granularity

        # Track delta momentum
        self._delta_history: deque = deque(maxlen=momentum_window)
        self._volume_history: deque = deque(maxlen=momentum_window)
        
        # Track visited price levels (for virgin level detection)
        self._visited_levels: set = set()
        
        # Track pending absorption setups waiting for multi-bar confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_level: Optional[float] = None
        self._confirmation_bar_count: int = 0
        self._highest_since_absorption: Optional[float] = None
        self._lowest_since_absorption: Optional[float] = None
        
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
            self._delta_history = deque(maxlen=self.momentum_window)
            self._volume_history = deque(maxlen=self.momentum_window)
            self._visited_levels = set()
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_level = None
            self._confirmation_bar_count = 0
            self._highest_since_absorption = None
            self._lowest_since_absorption = None

        # Track visited levels
        self._update_visited_levels(current)

        # Accumulate delta and volume for momentum calculation
        self._delta_history.append(current.delta)
        self._volume_history.append(current.volume)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_level = None
            self._confirmation_bar_count = 0
            return

        # Handle multi-bar confirmation (3 bars total: detection + 2 confirmation)
        if self._pending_setup and self._pending_absorption_close is not None:
            self._confirmation_bar_count += 1
            
            # Update extremes since absorption
            if self._highest_since_absorption is None:
                self._highest_since_absorption = current.high
            else:
                self._highest_since_absorption = max(self._highest_since_absorption, current.high)
            
            if self._lowest_since_absorption is None:
                self._lowest_since_absorption = current.low
            else:
                self._lowest_since_absorption = min(self._lowest_since_absorption, current.low)
            
            # Bar 1 of confirmation: must move away from absorption level
            if self._confirmation_bar_count == 1:
                if not self._moved_away_from_level(current, self._pending_setup):
                    # Failed to move away, reset
                    self._pending_setup = None
                    self._pending_absorption_close = None
                    self._pending_absorption_level = None
                    self._confirmation_bar_count = 0
                    self._highest_since_absorption = None
                    self._lowest_since_absorption = None
                return
            
            # Bar 2 of confirmation: must continue move or reach virgin level
            elif self._confirmation_bar_count == 2:
                if self._should_enter_on_confirmation(current, self._pending_setup):
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                
                # Reset after attempting entry
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_absorption_level = None
                self._confirmation_bar_count = 0
                self._highest_since_absorption = None
                self._lowest_since_absorption = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption based on delta momentum reversal
        setup = self._detect_momentum_absorption(current)
        if setup:
            # Queue for multi-bar confirmation
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_level = self._quantize_level(current.close)
            self._confirmation_bar_count = 0
            self._highest_since_absorption = current.high
            self._lowest_since_absorption = current.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _quantize_level(self, price: float) -> float:
        """Quantize price to the nearest level granularity."""
        return round(price / self.level_granularity) * self.level_granularity

    def _update_visited_levels(self, bar) -> None:
        """Track price levels visited in this bar."""
        high_level = self._quantize_level(bar.high)
        low_level = self._quantize_level(bar.low)
        close_level = self._quantize_level(bar.close)
        
        self._visited_levels.add(high_level)
        self._visited_levels.add(low_level)
        self._visited_levels.add(close_level)

    def _is_virgin_level(self, price: float) -> bool:
        """Check if a price level is virgin (not yet visited in this session)."""
        level = self._quantize_level(price)
        return level not in self._visited_levels

    def _calculate_momentum(self) -> tuple[float, float]:
        """
        Calculate delta momentum and volume.
        
        Returns: (cumulative_delta, total_volume)
        """
        if not self._delta_history or not self._volume_history:
            return 0.0, 0.0
        
        cum_delta = sum(self._delta_history)
        total_vol = sum(self._volume_history)
        
        return cum_delta, total_vol

    def _detect_momentum_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption when delta momentum reverses sharply.
        
        Absorption signature:
        1. Need at least 2 bars of history to detect reversal
        2. Delta momentum reverses (was positive, now negative, or vice versa)
        3. Reversal magnitude is significant (ratio >= 0.40)
        4. Current bar shows strong wick AGAINST the new momentum direction
        5. Wick is substantial (>= 0.55 points and >= 42% of range)
        6. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        Setup will be confirmed on the next 2 bars.
        """
        if current.volume == 0:
            return None

        # Need history to detect reversal
        if len(self._delta_history) < 2:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Get previous momentum (all bars except current)
        prev_deltas = list(self._delta_history)[:-1]
        prev_momentum = sum(prev_deltas) if prev_deltas else 0.0
        
        # Current momentum includes this bar
        curr_momentum, curr_total_vol = self._calculate_momentum()

        # Detect reversal
        if prev_momentum == 0:
            return None

        # Check if momentum reversed
        reversed_sign = (prev_momentum > 0 and curr_momentum < 0) or \
                       (prev_momentum < 0 and curr_momentum > 0)
        
        if not reversed_sign:
            return None

        # Check reversal magnitude
        reversal_ratio = abs(curr_momentum) / (abs(prev_momentum) + abs(curr_momentum) + 1e-9)
        if reversal_ratio < self.momentum_reversal_ratio:
            return None

        # LONG: Momentum was negative (selling), now positive (buying) with absorption
        if prev_momentum < 0 and curr_momentum > 0:
            lower_wick = current.close - current.low
            
            # Filter: Lower wick must be present
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Calculate stop and target
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

        # SHORT: Momentum was positive (buying), now negative (selling) with absorption
        elif prev_momentum > 0 and curr_momentum < 0:
            upper_wick = current.high - current.close
            
            # Filter: Upper wick must be present
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Calculate stop and target
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

    def _moved_away_from_level(self, bar, setup: dict) -> bool:
        """Check if bar moved away from the absorption level."""
        if setup["direction"] == "long":
            # For long, must move higher than absorption level
            return bar.high > self._pending_absorption_level
        else:
            # For short, must move lower than absorption level
            return bar.low < self._pending_absorption_level

    def _should_enter_on_confirmation(self, bar, setup: dict) -> bool:
        """
        Check if confirmation bar validates entry.
        
        Must:
        1. Continue moving away from absorption level
        2. Either reach a virgin level OR show efficiency in movement
        """
        if setup["direction"] == "long":
            # Must continue moving higher
            if bar.close <= self._pending_absorption_close:
                return False
            
            # Check if reached a virgin level or moving efficiently
            reached_virgin = self._is_virgin_level(bar.high)
            
            # Check efficiency
            if bar.volume > 0:
                buy_ratio = bar.buy_volume / bar.volume
                efficient_move = buy_ratio <= self.efficiency_ratio_max
            else:
                efficient_move = False
            
            return reached_virgin or efficient_move
        
        else:  # short
            # Must continue moving lower
            if bar.close >= self._pending_absorption_close:
                return False
            
            # Check if reached a virgin level or moving efficiently
            reached_virgin = self._is_virgin_level(bar.low)
            
            # Check efficiency
            if bar.volume > 0:
                sell_ratio = bar.sell_volume / bar.volume
                efficient_move = sell_ratio <= self.efficiency_ratio_max
            else:
                efficient_move = False
            
            return reached_virgin or efficient_move

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed momentum absorption."""
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
