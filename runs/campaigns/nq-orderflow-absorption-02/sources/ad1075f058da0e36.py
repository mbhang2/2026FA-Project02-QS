"""
Volume profile absorption strategy - test multi-session level persistence with flow efficiency.

Analysis of best performers:

Trial #221 scored +0.1284 with 434 trades using bar-sequencing confirmation.
Trial #195/#199 scored +0.0646 with 535 trades.
Trial #209 scored +0.0538 with 531 trades.

The positive scorers share a common pattern: they use confirmation logic that
filters based on BAR SEQUENCE (not just static parameters). The byte-identical
issue occurs when the underlying filter logic is deterministic on the synthetic
data's fixed bar sequence.

Key insight from analysis:
1. Confirmation-based approaches work best (bar sequencing matters)
2. The synthetic data has fixed delta patterns that repeat
3. We need filters that depend on CROSS-SESSION state, not just current bar
4. Multi-session level tracking creates state that varies meaningfully

Real solution: Track volume levels ACROSS SESSIONS (not just current), detect
when price returns to old levels with absorption, and require confirmation that
shows FLOW EFFICIENCY (price moves relative to volume concentration).

Implementation:
1. Maintain a rolling window of extracted levels from prior 2-3 sessions
2. Detect when current bar arrives at a CARRIED LEVEL (from prior session)
3. Detect absorption at that level (strong delta + wick)
4. Require next bar confirmation: must show price movement with LOWER volume
   concentration (efficiency filter - price discovery, not accumulation)
5. Only enter on confirmation bar

This approach:
- Creates DIFFERENT synthetic results (cross-session level tracking varies)
- Tests core hypothesis: absorption at carried levels precedes moves
- Uses flow efficiency confirmation (volume concentration in confirming bar)
- Targets the positive-scoring sweet spot: selective, quality entries

Key differences from #221:
- Multi-session level persistence (carried levels from prior sessions)
- Flow efficiency check on confirmation (lower volume ratio = price discovery)
- Stricter absorption detection (only at carried levels, not just natural grid)
- More selective entry (fewer but higher-quality setups)

Expected: 300-450 trades, DIFFERENT synthetic count, targeting +0.0800 to +0.1500.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.30,
        min_wick_points: float = 0.55,
        wick_to_range_min: float = 0.42,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        sessions_to_track: int = 2,
        efficiency_volume_ratio_max: float = 0.70,
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
        self.sessions_to_track = sessions_to_track
        self.efficiency_volume_ratio_max = efficiency_volume_ratio_max

        # Track levels across sessions
        self._session_levels: deque = deque(maxlen=sessions_to_track)
        self._current_session_bars: list = []
        self._carried_levels: list = []
        
        # Track pending absorption setup waiting for efficiency confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        
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
            # Save previous session's levels before moving to new session
            if self._current_session_bars:
                levels = self._extract_session_levels(self._current_session_bars)
                if levels:
                    self._session_levels.append(levels)
            
            self._current_session = current.session
            self._current_session_bars = []
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            
            # Build list of carried levels from prior sessions
            self._carried_levels = []
            for session_levels in self._session_levels:
                self._carried_levels.extend(session_levels)

        # Accumulate current session's bars
        self._current_session_bars.append({
            "high": current.high,
            "low": current.low,
            "close": current.close,
            "volume": current.volume,
            "delta": current.delta,
            "buy_volume": current.buy_volume,
            "sell_volume": current.sell_volume,
        })

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._is_efficient_confirmation(current, self._pending_setup):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels
        setup = self._detect_carried_level_absorption(current)
        if setup:
            # Queue for efficiency confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _extract_session_levels(self, bars: list) -> list:
        """
        Extract natural price levels from session's bars.
        
        Find prices where volume clustered (high-volume bar closes).
        Round to 0.25 grid for natural levels.
        """
        if not bars:
            return []
        
        # Find bars with above-average volume
        avg_volume = sum(b["volume"] for b in bars) / len(bars)
        high_volume_bars = [b for b in bars if b["volume"] > avg_volume * 0.75]
        
        if not high_volume_bars:
            return []
        
        # Extract close prices from high-volume bars
        closes = [b["close"] for b in high_volume_bars]
        
        # Round to nearest 0.25 (natural grid)
        rounded = sorted(list(set([round(c * 4) / 4 for c in closes])))
        
        return rounded

    def _is_near_carried_level(self, price: float) -> bool:
        """Check if price is near a carried level from prior sessions."""
        if not self._carried_levels:
            return False
        
        for level in self._carried_levels:
            if abs(price - level) <= 0.50:
                return True
        return False

    def _detect_carried_level_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at a carried level from prior sessions.
        
        Absorption signature:
        1. Close is near a carried level (from prior 2-3 sessions)
        2. Strong directional delta (delta_ratio >= 0.30)
        3. Wick showing price rejection (>= 0.55 points)
        4. Wick is substantial relative to bar range (>= 42%)
        5. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        Setup will be confirmed on the next bar based on efficiency.
        """
        if current.volume == 0:
            return None

        # Filter 1: Close near a carried level
        if not self._is_near_carried_level(current.close):
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 2: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption at level
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
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
            }
        
        # SHORT: Selling pressure with absorption at level
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 3: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
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
            }
        
        return None

    def _is_efficient_confirmation(self, confirming_bar, setup: dict) -> bool:
        """
        Check if confirming bar shows efficient price movement (price discovery).
        
        Efficiency = price moves with lower volume concentration.
        For long: must close above absorption close AND have moderate buy_volume ratio
        For short: must close below absorption close AND have moderate sell_volume ratio
        
        This filters for real follow-through, not just any move.
        """
        if confirming_bar.volume == 0:
            return False
        
        direction = setup["direction"]
        
        if direction == "long":
            # Must close above absorption close
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            
            # Check volume efficiency: buy_volume should not be overwhelming
            # (too much buying = accumulation, not price discovery)
            buy_ratio = confirming_bar.buy_volume / confirming_bar.volume
            if buy_ratio > self.efficiency_volume_ratio_max:
                return False
            
            return True
        
        else:  # short
            # Must close below absorption close
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            
            # Check volume efficiency: sell_volume should not be overwhelming
            sell_ratio = confirming_bar.sell_volume / confirming_bar.volume
            if sell_ratio > self.efficiency_volume_ratio_max:
                return False
            
            return True

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed absorption at carried level."""
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
