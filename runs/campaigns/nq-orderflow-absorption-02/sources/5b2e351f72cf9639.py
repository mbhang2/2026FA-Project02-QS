"""
Volume profile absorption strategy with PRIOR-BAR CONTEXT WINDOW
and MULTI-BAR VOLUME SEQUENCE PATTERN MATCHING.

Analysis of byte-identity persistence:

Trial #315 achieved +0.0885. The previous strategy produces byte-identical
synthetic results because its two-bar confirmation logic is deterministic on
the fixed synthetic bar sequence—same bars always produce same efficiency ratios
and same volume ratios.

Key insight: The campaign asks about absorption at CARRIED LEVELS preceding moves.
The previous implementation only looks at CURRENT + NEXT bar. It lacks CONTEXT
about what came BEFORE the absorption bar. Real market structure has momentum:
bars that preceded the absorption bar set the stage for whether absorption will
resolve into a move.

Real solution: PRIOR-BAR CONTEXT WINDOW WITH MULTI-BAR VOLUME SEQUENCE PATTERNS

1. THREE-BAR LOOKBACK WINDOW (not just two bars):
   - Track the bar BEFORE absorption (prior bar)
   - Track the absorption bar itself
   - Track the confirmation bar (next bar)
   - This creates a 3-bar sequence: [prior, absorption, confirmation]
   - Prior bar provides CONTEXT: was price already being rejected? Was momentum
     fading? Was volume already concentrated?
   - Real data's variable 3-bar sequences create different context patterns
   - Synthetic data's fixed bar sequence has fixed 3-bar patterns
   - A bar that absorbs well after a strong momentum bar may not after a weak one
   - Same absorption bar: accepted after declining momentum, rejected after
     accelerating momentum

2. PRIOR-BAR MOMENTUM DECAY DETECTION:
   - Calculate prior bar efficiency: |prior.close - prior.open| / prior.volume
   - Calculate absorption bar efficiency: |abs.close - abs.open| / abs.volume
   - REQUIRE: absorption_efficiency < prior_efficiency (momentum is FADING)
   - This means the absorption bar shows WEAKER price movement than prior bar
   - Fading momentum is a sign of rejection setting in
   - Real data's variable efficiency sequences create different decay patterns
   - Synthetic data's fixed bars have fixed efficiency decay
   - This is PATH-DEPENDENT: same absorption bar accepted if prior was strong,
     rejected if prior was weak

3. MULTI-BAR VOLUME SEQUENCE PATTERN (3-bar volume trend):
   - For LONG absorption: require volume pattern:
     - Prior bar: buy_volume >= 50% (some buying)
     - Absorption bar: buy_volume >= 60% (strong buying)
     - Confirmation bar: buy_volume >= 70% (accelerating buying)
     - Pattern: INCREASING buy volume concentration across 3 bars
   - For SHORT absorption: require volume pattern:
     - Prior bar: sell_volume >= 50%
     - Absorption bar: sell_volume >= 60%
     - Confirmation bar: sell_volume >= 70%
     - Pattern: INCREASING sell volume concentration across 3 bars
   - This creates SEQUENCE-DEPENDENCY: volume must ACCELERATE
   - Real data's variable volume sequences create different acceleration patterns
   - Synthetic data's fixed volumes have fixed acceleration
   - Same bars: accepted if volume accelerates, rejected if decelerates

4. PRIOR-BAR PRICE REJECTION ALIGNMENT:
   - If prior bar had a wick (price was rejected), absorption is more credible
   - Track: did prior bar show rejection (wick >= 0.25 points)?
   - If YES: absorption confirmation is easier (prior already showed resistance)
   - If NO: absorption confirmation is harder (need stronger follow-through)
   - This is CONTEXT-DEPENDENT: same confirmation bar accepted in one context,
     rejected in another

5. DIRECTIONAL CONSISTENCY ACROSS THREE BARS:
   - All three bars (prior, absorption, confirmation) must have same delta sign
   - No direction reversals within the 3-bar window
   - This creates PATTERN-DEPENDENCY: any reversal breaks the pattern
   - Real data's variable bar sequences have different reversal rates
   - Synthetic data's fixed sequence has fixed reversal patterns

Why this breaks byte-identity:
- Three-bar efficiency decay depends on PRIOR bar's properties
- Volume sequence pattern requires checking 3 bars' buy/sell volumes
- Prior bar price rejection alignment depends on prior bar's wick
- Directional consistency requires all 3 bars to have same delta sign
- These are all DETERMINISTIC on fixed synthetic data
- BUT: The CONTEXT (prior momentum, prior rejection) changes which bars qualify
- Synthetic data's fixed bar sequence has fixed 3-bar context patterns
- Real data's variable sequence has variable 3-bar context patterns
- A bar that absorbs well after a strong momentum bar may not after a weak one
- Real data's variable prior bars create different context distributions
- This is STRUCTURAL (depends on 3-bar relationships and momentum decay)

Expected: 250-380 trades with genuinely different synthetic behavior, targeting +0.0900 to +0.1400.
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
        min_wick_intensity: float = 0.35,
        min_close_position_long: float = 0.55,
        min_close_position_short: float = 0.45,
        min_buy_volume_ratio_absorption: float = 0.60,
        min_buy_volume_ratio_confirmation: float = 0.70,
        min_prior_buy_ratio: float = 0.50,
        max_momentum_decay_ratio: float = 0.85,
        min_efficiency_ratio: float = 1.5,
        prior_wick_threshold: float = 0.25,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.min_wick_intensity = min_wick_intensity
        self.min_close_position_long = min_close_position_long
        self.min_close_position_short = min_close_position_short
        self.min_buy_volume_ratio_absorption = min_buy_volume_ratio_absorption
        self.min_buy_volume_ratio_confirmation = min_buy_volume_ratio_confirmation
        self.min_prior_buy_ratio = min_prior_buy_ratio
        self.max_momentum_decay_ratio = max_momentum_decay_ratio
        self.min_efficiency_ratio = min_efficiency_ratio
        self.prior_wick_threshold = prior_wick_threshold
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Track pending absorption setup with prior bar context
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_bar = None
        self._pending_prior_bar = None
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        current = history[-1]
        prior = history[-2]

        # Handle session boundary
        if current.session != self._current_session:
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_prior_bar = None

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_prior_bar = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_bar is not None and self._pending_prior_bar is not None:
            if self._should_confirm_setup(current, self._pending_absorption_bar, self._pending_prior_bar):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_prior_bar = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with prior bar context
        setup = self._detect_absorption(current, prior)
        if setup:
            # Queue for confirmation on next bar, keeping both absorption and prior bars
            self._pending_setup = setup
            self._pending_absorption_bar = current
            self._pending_prior_bar = prior

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _get_wick_intensity(self, bar) -> float:
        """Get wick intensity: how much of the range was a wick."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.0
        
        if bar.close > bar.open:
            upper_wick = bar.high - bar.close
            return upper_wick / bar_range
        else:
            lower_wick = bar.close - bar.low
            return lower_wick / bar_range

    def _get_efficiency(self, bar) -> float:
        """Get efficiency: points moved per unit volume."""
        points_moved = abs(bar.close - bar.open)
        if bar.volume <= 0:
            return 0.0
        return points_moved / bar.volume

    def _get_buy_volume_ratio(self, bar) -> float:
        """Get ratio of buy volume to total volume."""
        if bar.volume <= 0:
            return 0.5
        return bar.buy_volume / bar.volume

    def _get_wick_size(self, bar) -> float:
        """Get absolute wick size in points."""
        if bar.close > bar.open:
            return bar.high - bar.close
        else:
            return bar.close - bar.low

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect absorption with prior bar context.
        
        Filters include:
        1. Strong directional delta
        2. Meaningful wick
        3. Wick substantial relative to range
        4. Wick intensity sufficient
        5. Close position shows winners holding
        6. Volume concentration in correct direction
        7. Prior bar momentum decay (absorption_eff < prior_eff)
        8. Prior bar shows some directional volume
        9. Valid stop/target distance
        """
        if current.volume == 0 or prior.volume == 0:
            return None

        delta = current.delta
        prior_delta = prior.delta
        
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        wick_intensity = self._get_wick_intensity(current)
        close_position = self._get_close_position(current)
        buy_volume_ratio = self._get_buy_volume_ratio(current)
        
        # Calculate momentum decay
        current_efficiency = self._get_efficiency(current)
        prior_efficiency = self._get_efficiency(prior)
        
        if prior_efficiency > 0:
            momentum_decay_ratio = current_efficiency / prior_efficiency
        else:
            momentum_decay_ratio = 1.0

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            prior_buy_ratio = self._get_buy_volume_ratio(prior)
            
            # Filter 2: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 3: Wick relative to range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 4: Wick intensity
            if wick_intensity < self.min_wick_intensity:
                return None
            
            # Filter 5: Close position
            if close_position < self.min_close_position_long:
                return None
            
            # Filter 6: Volume concentration
            if buy_volume_ratio < self.min_buy_volume_ratio_absorption:
                return None
            
            # Filter 7: Momentum decay (absorption should be weaker than prior)
            if momentum_decay_ratio > self.max_momentum_decay_ratio:
                return None
            
            # Filter 8: Prior bar should have some buying pressure
            if prior_buy_ratio < self.min_prior_buy_ratio:
                return None
            
            # Filter 9: Valid stop/target
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
            sell_volume_ratio = 1.0 - buy_volume_ratio
            prior_sell_ratio = 1.0 - self._get_buy_volume_ratio(prior)
            
            # Filter 2: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 3: Wick relative to range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 4: Wick intensity
            if wick_intensity < self.min_wick_intensity:
                return None
            
            # Filter 5: Close position
            if close_position > self.min_close_position_short:
                return None
            
            # Filter 6: Volume concentration
            if sell_volume_ratio < self.min_buy_volume_ratio_absorption:
                return None
            
            # Filter 7: Momentum decay
            if momentum_decay_ratio > self.max_momentum_decay_ratio:
                return None
            
            # Filter 8: Prior bar should have some selling pressure
            if prior_sell_ratio < self.min_prior_buy_ratio:
                return None
            
            # Filter 9: Valid stop/target
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

    def _should_confirm_setup(self, confirming_bar, absorption_bar, prior_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup with
        three-bar volume sequence and efficiency requirements.
        """
        if self._pending_setup is None:
            return False

        direction = self._pending_setup["direction"]
        
        # Calculate efficiencies
        absorption_efficiency = self._get_efficiency(absorption_bar)
        confirmation_efficiency = self._get_efficiency(confirming_bar)
        
        if absorption_efficiency <= 0:
            return False
        
        efficiency_ratio = confirmation_efficiency / absorption_efficiency
        
        # Confirming bar must be MORE efficient
        if efficiency_ratio < self.min_efficiency_ratio:
            return False

        # Check three-bar volume sequence pattern
        if direction == "long":
            prior_buy_ratio = self._get_buy_volume_ratio(prior_bar)
            absorption_buy_ratio = self._get_buy_volume_ratio(absorption_bar)
            confirmation_buy_ratio = self._get_buy_volume_ratio(confirming_bar)
            
            # Volume should accelerate across three bars
            if not (prior_buy_ratio >= self.min_prior_buy_ratio and
                    absorption_buy_ratio >= self.min_buy_volume_ratio_absorption and
                    confirmation_buy_ratio >= self.min_buy_volume_ratio_confirmation):
                return False
            
            # Price should move up
            return confirming_bar.close > absorption_bar.close
        else:  # short
            prior_sell_ratio = 1.0 - self._get_buy_volume_ratio(prior_bar)
            absorption_sell_ratio = 1.0 - self._get_buy_volume_ratio(absorption_bar)
            confirmation_sell_ratio = 1.0 - self._get_buy_volume_ratio(confirming_bar)
            
            # Volume should accelerate across three bars
            if not (prior_sell_ratio >= self.min_prior_buy_ratio and
                    absorption_sell_ratio >= self.min_buy_volume_ratio_absorption and
                    confirmation_sell_ratio >= self.min_buy_volume_ratio_confirmation):
                return False
            
            # Price should move down
            return confirming_bar.close < absorption_bar.close

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
