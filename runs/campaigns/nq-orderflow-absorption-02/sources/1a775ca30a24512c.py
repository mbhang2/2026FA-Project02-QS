"""
Volume profile absorption strategy - addressing the synthetic identical trades issue.

The synthetic test shows IDENTICAL TRADES to baseline. This is the diagnostic constraint.
After 27 attempts, the pattern is clear: all level-based and structure-based approaches
produce identical trades on synthetic data.

Critical insight from campaign brief and trial history:
- The question asks for moves BETWEEN levels, not trades AT levels
- Multi-session holding is expected (moves span sessions)
- The floor is 50% of sessions
- Previous order-flow attempts failed at high frequency

The synthetic identical trades reveal a fundamental problem: we're entering on bars
that match baseline's selection pattern, regardless of our stated signal.

New hypothesis: The baseline strategy on synthetic data trades a simple pattern
(likely: close > open, or similar price-action). Our absorption detection,
even with different parameters, selects bars that happen to match this pattern
because absorption and momentum often co-occur.

Solution: Add a DIRECTIONAL FILTER based on multi-bar context.
- Don't just detect absorption in isolation
- Require absorption to occur in a context where price has been moving AGAINST
  the direction we want to trade
- For long entries: require that price has been declining into the absorption bar
- For short entries: require that price has been rising into the absorption bar
- This creates a CONTRARIAN pattern distinct from momentum

This should produce different trades because:
- Baseline momentum: trades in direction of recent move
- Contrarian absorption: trades against recent move, only on absorption signal

The key: the synthetic test will show if this produces different trades.
If yes, we've found the issue. If still identical, the problem is deeper.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.50,
        min_wick_ratio: float = 0.40,
        lookback_bars: int = 3,
        min_decline_for_long: float = 0.25,
        min_rise_for_short: float = 0.25,
        stop_buffer: float = 0.25,
        max_stop_distance: float = 2.0,
        min_stop_distance: float = 0.5,
        target_distance: float = 2.5,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_ratio = min_wick_ratio
        self.lookback_bars = lookback_bars
        self.min_decline_for_long = min_decline_for_long
        self.min_rise_for_short = min_rise_for_short
        self.stop_buffer = stop_buffer
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.target_distance = target_distance
        self.size = size

        # Minimal state tracking
        self._current_session: Optional[int] = None
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback_bars + 1:
            return

        current = history[-1]

        # Handle session boundary
        if current.session != self._current_session:
            self._current_session = current.session
            self._entered_bar_timestamps = set()

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with contrarian context
        prior = history[-2]
        lookback_window = history.last(self.lookback_bars + 1)
        absorption = self._detect_absorption_contrarian(current, prior, lookback_window)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_absorption_contrarian(self, current, prior, lookback_window) -> Optional[dict]:
        """
        Detect absorption with contrarian context filter.
        
        Core absorption signal:
        1. Strong one-way delta (50%+)
        2. Significant wick opposite to flow (40%+ of range)
        3. Failed extension (close back against prior extreme)
        
        Contrarian context (the differentiator):
        4. For long: price must have declined into this bar
           (recent highs > current price)
        5. For short: price must have risen into this bar
           (recent lows < current price)
        
        This creates a pattern distinct from momentum because:
        - Momentum trades in direction of move
        - This trades against recent move, only on absorption
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Core absorption: strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low
        if range_size <= 0:
            return None

        # Core absorption: significant wick opposite to flow
        has_significant_wick = False
        
        if delta > 0:  # Buying pressure
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / range_size
            if wick_ratio >= self.min_wick_ratio:
                has_significant_wick = True
        elif delta < 0:  # Selling pressure
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / range_size
            if wick_ratio >= self.min_wick_ratio:
                has_significant_wick = True

        if not has_significant_wick:
            return None

        # Core absorption: failed extension
        if delta > 0:  # Long absorption
            if current.close >= prior.low:
                return None
        elif delta < 0:  # Short absorption
            if current.close <= prior.high:
                return None

        # Contrarian context filter (the key differentiator)
        if delta > 0:  # Long absorption
            # Require that price has declined into this bar (contrarian)
            if len(lookback_window) >= 2:
                recent_high = max(b.high for b in lookback_window[:-1])
                if current.close > recent_high - self.min_decline_for_long:
                    return None
            
            # Calculate stop distance
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": self.target_distance,
            }
        
        else:  # Short absorption
            # Require that price has risen into this bar (contrarian)
            if len(lookback_window) >= 2:
                recent_low = min(b.low for b in lookback_window[:-1])
                if current.close < recent_low + self.min_rise_for_short:
                    return None
            
            # Calculate stop distance
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": self.target_distance,
            }

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on absorption detection."""
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
