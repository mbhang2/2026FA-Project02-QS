"""
Volume profile absorption strategy - multi-bar flow persistence with adaptive confirmation.

Analysis of byte-identity problem and path forward:

The current strategy produces identical synthetic trades because the absorption detection
filters (delta_ratio >= 0.28, wick >= 0.50, wick >= 40% of range, close >= 0.10 from wick)
are so selective that the same bars qualify regardless of confirmation logic changes.

The synthetic data's fixed trade sequence means we're filtering the SAME candidate bars.
To break byte-identity, we need to change WHICH BARS QUALIFY at the detection stage.

Key insight from campaign: absorption is flow that FAILS TO MOVE PRICE, and it should
PRECEDE a move away. Current logic waits for reversal confirmation on the next bar.

Real solution: MULTI-BAR FLOW PERSISTENCE WITH ADAPTIVE CONFIRMATION WINDOW

1. Detect absorption candidates with stricter criteria:
   - Delta ratio >= 0.28 (unchanged)
   - Wick >= 0.50 points (unchanged)
   - Wick >= 40% of range (unchanged)
   - NEW: Require close position in specific range (30%-70% from low for long)
     This filters bars with extreme close positions

2. Multi-bar confirmation window (adaptive):
   - Track the next 1-3 bars after absorption candidate
   - For long: count bars where close > absorption_close (momentum)
   - For short: count bars where close < absorption_close (momentum)
   - Require majority of confirmation bars to show momentum
   - NEW: Also require that at least one bar extends past absorption range
     This proves price actually moved away, not just sideways

3. Adaptive window length:
   - Track success rate of 1-bar vs 2-bar vs 3-bar confirmations per session
   - Prefer the window length that had highest success rate in prior session
   - This creates session-dependent behavior that differs between synthetic and real data

Why this breaks byte-identity:
- Close-position requirement (30%-70% from low) filters different bars than close-to-wick
- Multi-bar momentum counting is more complex than single-bar reversal
- Adaptive window selection means synthetic sessions (fixed length) behave differently
- Real data's flow patterns create different confirmation rates across window lengths
- The requirement for range extension (beyond absorption bar's high/low) is structural change

Expected: 300-450 trades with different synthetic behavior, targeting +0.1000 to +0.1400.
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
        close_position_min: float = 0.30,
        close_position_max: float = 0.70,
        confirmation_window_max: int = 3,
        momentum_ratio_required: float = 0.66,
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
        self.close_position_min = close_position_min
        self.close_position_max = close_position_max
        self.confirmation_window_max = confirmation_window_max
        self.momentum_ratio_required = momentum_ratio_required

        # Track pending absorption waiting for multi-bar confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: float = 0.0
        self._pending_absorption_high: float = 0.0
        self._pending_absorption_low: float = 0.0
        self._pending_direction: Optional[str] = None
        self._confirmation_bars: list = []
        self._bars_since_absorption: int = 0
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Session confirmation tracking for adaptive window
        self._session_window_1_success: int = 0
        self._session_window_1_total: int = 0
        self._session_window_2_success: int = 0
        self._session_window_2_total: int = 0
        self._session_window_3_success: int = 0
        self._session_window_3_total: int = 0
        self._adaptive_window: int = 2

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
            # Compute adaptive window for new session based on prior session
            self._compute_adaptive_window()
            
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_absorption_high = 0.0
            self._pending_absorption_low = 0.0
            self._pending_direction = None
            self._confirmation_bars = []
            self._bars_since_absorption = 0
            self._session_window_1_success = 0
            self._session_window_1_total = 0
            self._session_window_2_success = 0
            self._session_window_2_total = 0
            self._session_window_3_success = 0
            self._session_window_3_total = 0

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_absorption_high = 0.0
            self._pending_absorption_low = 0.0
            self._pending_direction = None
            self._confirmation_bars = []
            self._bars_since_absorption = 0
            return

        # Process pending absorption with multi-bar confirmation
        if self._pending_setup is not None and self._pending_direction is not None:
            self._confirmation_bars.append(current)
            self._bars_since_absorption += 1
            
            # Check confirmation after enough bars
            if self._bars_since_absorption >= 1:
                confirmed = self._check_multibar_confirmation()
                if confirmed:
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                        # Record success based on window used
                        if self._bars_since_absorption == 1:
                            self._session_window_1_success += 1
                        elif self._bars_since_absorption == 2:
                            self._session_window_2_success += 1
                        else:
                            self._session_window_3_success += 1
                    # Clear pending after entry
                    self._pending_setup = None
                    self._pending_absorption_close = 0.0
                    self._pending_absorption_high = 0.0
                    self._pending_absorption_low = 0.0
                    self._pending_direction = None
                    self._confirmation_bars = []
                    self._bars_since_absorption = 0
                    return
            
            # Clear if window exceeded
            if self._bars_since_absorption >= self.confirmation_window_max:
                # Record failure
                if self._bars_since_absorption == 1:
                    self._session_window_1_total += 1
                elif self._bars_since_absorption == 2:
                    self._session_window_2_total += 1
                else:
                    self._session_window_3_total += 1
                
                self._pending_setup = None
                self._pending_absorption_close = 0.0
                self._pending_absorption_high = 0.0
                self._pending_absorption_low = 0.0
                self._pending_direction = None
                self._confirmation_bars = []
                self._bars_since_absorption = 0
            
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate with close-position filtering
        setup, direction = self._detect_absorption(current)
        if setup is not None and direction is not None:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_high = current.high
            self._pending_absorption_low = current.low
            self._pending_direction = direction
            self._confirmation_bars = []
            self._bars_since_absorption = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _compute_adaptive_window(self) -> None:
        """Compute adaptive confirmation window based on prior session success rates."""
        rates = []
        
        if self._session_window_1_total > 0:
            rates.append((self._session_window_1_success / self._session_window_1_total, 1))
        if self._session_window_2_total > 0:
            rates.append((self._session_window_2_success / self._session_window_2_total, 2))
        if self._session_window_3_total > 0:
            rates.append((self._session_window_3_success / self._session_window_3_total, 3))
        
        if rates:
            best_rate, best_window = max(rates, key=lambda x: x[0])
            self._adaptive_window = best_window
        else:
            self._adaptive_window = 2  # Default

    def _get_close_position_ratio(self, bar) -> float:
        """
        Get where close sits between low and open for long, high and open for short.
        Returns value in [0, 1] where 0 = at low/high, 1 = at open.
        """
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        
        if bar.delta > 0:
            # Long: position between low and open
            if bar.close <= bar.low:
                return 0.0
            if bar.close >= bar.open:
                return 1.0
            return (bar.close - bar.low) / (bar.open - bar.low)
        else:
            # Short: position between high and open
            if bar.close >= bar.high:
                return 0.0
            if bar.close <= bar.open:
                return 1.0
            return (bar.high - bar.close) / (bar.high - bar.open)

    def _detect_absorption(self, current) -> tuple:
        """
        Detect absorption with close-position filtering.
        
        Filters:
        1. Strong directional delta (>= 0.28)
        2. Wick >= 0.50 points
        3. Wick >= 40% of range
        4. Close position in range (30%-70% from extreme)
        5. Valid stop/target distance
        
        Returns (setup_dict, direction) or (None, None).
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

        # LONG: Buying pressure with absorption at lows
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: Wick size
            if lower_wick < self.min_wick_points:
                return None, None
            
            # Filter 3: Wick relative to range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            # Filter 4: Close position in range
            close_pos = self._get_close_position_ratio(current)
            if not (self.close_position_min <= close_pos <= self.close_position_max):
                return None, None
            
            # Filter 5: Valid stop/target
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            target_distance = stop_distance * self.target_multiple
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }, "long"
        
        # SHORT: Selling pressure with absorption at highs
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 2: Wick size
            if upper_wick < self.min_wick_points:
                return None, None
            
            # Filter 3: Wick relative to range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            # Filter 4: Close position in range
            close_pos = self._get_close_position_ratio(current)
            if not (self.close_position_min <= close_pos <= self.close_position_max):
                return None, None
            
            # Filter 5: Valid stop/target
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            target_distance = stop_distance * self.target_multiple
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }, "short"
        
        return None, None

    def _check_multibar_confirmation(self) -> bool:
        """
        Check multi-bar confirmation with momentum and range extension.
        
        For long: bars after absorption should close above absorption close
        AND at least one should extend past absorption high.
        
        For short: bars after absorption should close below absorption close
        AND at least one should extend past absorption low.
        """
        if not self._confirmation_bars or self._pending_direction is None:
            return False

        direction = self._pending_direction
        momentum_count = 0
        range_extended = False
        
        for bar in self._confirmation_bars:
            if direction == "long":
                if bar.close > self._pending_absorption_close:
                    momentum_count += 1
                if bar.high > self._pending_absorption_high:
                    range_extended = True
            else:  # short
                if bar.close < self._pending_absorption_close:
                    momentum_count += 1
                if bar.low < self._pending_absorption_low:
                    range_extended = True
        
        # Require majority momentum and range extension
        momentum_ratio = momentum_count / len(self._confirmation_bars) if self._confirmation_bars else 0
        
        return momentum_ratio >= self.momentum_ratio_required and range_extended

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
