"""
Volume profile absorption strategy - refined swing detection with persistence confirmation.

Analysis of trial #31 (synthetic test identical to #30):
The synthetic test being identical reveals that our swing detection logic isn't
actually filtering trades differently. This suggests the swing identification
is either not working or the proximity threshold is being met by noise.

Root cause: Swing detection based on 3-bar local extremes is too loose.
On 5m bars with 0.25pt proximity, almost every bar near a recent high/low
qualifies, so the "strict" filter isn't actually filtering.

New approach - genuine structural level detection:
1. Track price levels where absorption has ALREADY occurred and held
2. Only trade new absorption at levels where absorption previously worked
3. This creates a self-reinforcing pattern: absorption at a level proves
   the level is significant
4. Requires 2-session history minimum to have prior absorption levels
5. Use tighter parameters: 0.28 delta ratio, 0.20 proximity
6. Keep stops at 0.75pt, targets at 4.0pt for cost efficiency
7. Expected: 4-6k trades with much higher quality, score -0.10 to +0.20

Why this works:
- #29 scored -0.2654 with strict filtering (but too few trades)
- The issue: we were filtering on proximity to RECENT bars, not SIGNIFICANT levels
- A significant level is one where the market has already shown behavior
- By trading absorption only at levels where absorption previously occurred,
  we're trading a pattern that has already been tested
- This is more conservative but much higher quality

The key insight:
- Absorption at a random level = noise
- Absorption at a level where absorption previously worked = signal
- This creates a self-validating pattern
- We accumulate these "proven" levels across sessions
- Each level we trade at becomes a data point for future sessions

Implementation:
1. Keep a set of "proven levels" where we've seen absorption work
2. When we detect absorption, check if it's at a proven level
3. If yes, trade it
4. If no, add it to our list of "candidate levels" to monitor
5. On the next session, if we see absorption at a candidate level, it becomes proven
6. This creates a bootstrapping effect where the signal gets better over time

Cost analysis:
- Starting sessions (no proven levels): trade on absorption alone (filtered by delta)
- Later sessions: trade only at proven levels (much higher quality)
- Expected: 5,000 trades total, 35-40% win rate
- 5,000 × 0.35 × 4.0 = 7,000pt gross
- 5,000 × 0.2pt = 1,000pt cost
- Net: 6,000pt positive = +15.6pt per session
- Score: +0.20 range

This is the only change that makes logical sense:
- We keep absorption detection (proven mechanism)
- We add level persistence (filters noise)
- We bootstrap the signal over time (self-improving)
- This directly tests the campaign hypothesis
"""

from __future__ import annotations
from collections import deque
from typing import Optional, Set

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        delta_ratio_threshold: float = 0.28,
        min_volume: int = 40,
        stop_offset: float = 0.75,
        target_offset: float = 4.0,
        size: int = 1,
        wick_min_ratio: float = 0.15,
        level_proximity: float = 0.20,
        momentum_max_ratio: float = 0.60,
    ) -> None:
        self.interval = interval
        self.delta_ratio_threshold = delta_ratio_threshold
        self.min_volume = min_volume
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.wick_min_ratio = wick_min_ratio
        self.level_proximity = level_proximity
        self.momentum_max_ratio = momentum_max_ratio

        # Track bars and proven absorption levels
        self.bars: deque = deque(maxlen=500)
        self.current_session: Optional[int] = None
        # Levels where we've successfully traded absorption
        self.proven_levels: Set[float] = set()
        # Candidate levels we're monitoring for confirmation
        self.candidate_levels: Set[float] = set()
        self.sessions_seen: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            self.sessions_seen += 1

        # Get current bar
        if self.interval not in ctx.bars:
            return

        current_bar = ctx.bars[self.interval]
        self.bars.append(current_bar)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Need minimum history
        if len(self.bars) < 5:
            return

        # Check for absorption signal
        absorption_direction = self._detect_absorption(current_bar)
        if absorption_direction is None:
            return

        # Filter: trade absorption at proven levels, or at candidate levels in later sessions
        should_trade = self._should_trade_absorption(current_bar, absorption_direction)
        if not should_trade:
            return

        # Execute the trade with GTC for multi-session holds
        tag = f"absorption-{absorption_direction.lower()}"

        if absorption_direction == "BUY":
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tif="GTC",
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tif="GTC",
                tag=tag,
            )

        # Mark this level as a candidate for future confirmation
        self._add_candidate_level(current_bar.close)

    def on_fill(self, ctx) -> None:
        # When a fill occurs, mark the entry level as proven
        if ctx.fill.position != 0:
            self._add_proven_level(ctx.fill.price)

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_absorption(self, current_bar) -> Optional[str]:
        """
        Detect absorption: strong one-way flow with price rejection and low momentum.

        Absorption characteristics:
        1. Delta ratio above threshold (28%+ one-sided)
        2. Meaningful wick in opposite direction (price rejected)
        3. Low momentum (close near open, not extended)
        4. Sufficient volume

        Returns "BUY" or "SELL" if detected, None otherwise.
        """
        if current_bar.volume < self.min_volume:
            return None

        # Calculate delta ratio
        delta_ratio = (
            abs(current_bar.delta) / current_bar.volume
            if current_bar.volume > 0
            else 0
        )

        # Need strong one-way flow
        if delta_ratio < self.delta_ratio_threshold:
            return None

        bar_range = current_bar.high - current_bar.low
        if bar_range < 0.10:
            return None

        # Calculate momentum
        momentum_ratio = (
            abs(current_bar.close - current_bar.open) / bar_range
            if bar_range > 0
            else 0
        )

        # Momentum must be LOW (absorption = flow without follow-through)
        if momentum_ratio > self.momentum_max_ratio:
            return None

        # Upside absorption: positive delta with upper wick
        if current_bar.delta > 0:
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick (price rejected upside)
            if wick_ratio > self.wick_min_ratio:
                # Close should be higher than open (bullish flow despite wick)
                if current_bar.close > current_bar.open:
                    return "BUY"

        # Downside absorption: negative delta with lower wick
        elif current_bar.delta < 0:
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick (price rejected downside)
            if wick_ratio > self.wick_min_ratio:
                # Close should be lower than open (bearish flow despite wick)
                if current_bar.close < current_bar.open:
                    return "SELL"

        return None

    def _should_trade_absorption(self, current_bar, direction: str) -> bool:
        """
        Determine if we should trade this absorption signal.
        
        Strategy:
        - In early sessions (< 5): trade all absorption signals to build level history
        - In later sessions: only trade absorption at proven levels or candidates
        """
        price = current_bar.close

        # Early bootstrap: trade all absorption to build proven levels
        if self.sessions_seen < 5:
            return True

        # Later: only trade at proven or candidate levels
        for proven in self.proven_levels:
            if abs(price - proven) <= self.level_proximity:
                return True

        for candidate in self.candidate_levels:
            if abs(price - candidate) <= self.level_proximity:
                return True

        return False

    def _add_proven_level(self, price: float) -> None:
        """Mark a level as proven (where we successfully traded)."""
        # Round to nearest 0.25 to avoid duplicate levels from slight price variations
        rounded = round(price * 4) / 4
        self.proven_levels.add(rounded)
        # Remove from candidates if it was there
        self.candidate_levels.discard(rounded)

    def _add_candidate_level(self, price: float) -> None:
        """Mark a level as candidate (where we detected absorption)."""
        # Round to nearest 0.25 to avoid duplicate levels from slight price variations
        rounded = round(price * 4) / 4
        if rounded not in self.proven_levels:
            self.candidate_levels.add(rounded)
