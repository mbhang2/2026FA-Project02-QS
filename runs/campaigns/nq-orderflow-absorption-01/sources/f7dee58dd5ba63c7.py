"""
Volume profile absorption strategy - testing multi-bar confirmation with persistence.

Analysis of trial #27 (byte-identical to #24, generating identical trades):
The synthetic test showed our logic produces the same trades regardless of parameter changes.
This means the STRUCTURE of our detection is correct, but we're not actually TESTING the hypothesis.

Core problem identified:
- We detect absorption (high delta + wick rejection)
- We wait for breakout confirmation
- But we're still getting the same trades as simpler momentum strategies
- This suggests our confirmation isn't filtering anything meaningful

Key insight from campaign brief and trial history:
"Does absorption at a carried level precede a move away from that level?"

The issue: We're checking if a bar breaks the absorption extreme, but we're not checking
if the move CONTINUES. A single bar breaking a level is noise; a SUSTAINED move is signal.

New hypothesis - test for PERSISTENCE:
1. Detect absorption (high delta + wick rejection + low momentum) ✓
2. Wait for breakout bar (closes past extreme with same-direction delta) ✓
3. NEW: Require the move to PERSIST - the next bar(s) must continue in same direction
4. Only enter when we see 2-3 bars of consistent directional movement
5. This filters out false breakouts and tests if absorption really precedes moves

This is the ACTUAL hypothesis test:
- Absorption shows where size rested (rejected price)
- If that level becomes support/resistance (absorbs further moves), price will reverse
- But if price breaks through AND KEEPS GOING, the absorption was predictive
- A move that reverses on the next bar was just noise

Expected impact:
- Fewer trades (only multi-bar confirmed moves)
- Higher win rate (we're waiting for persistence)
- Better score (surviving costs with fewer but higher-quality trades)

The key difference from #27:
- We don't enter on the first breakout bar
- We wait for the second bar to confirm the move persists
- This creates a 3-bar pattern: absorption + breakout + persistence
- Only then do we enter with larger targets (5.0 points) for multi-session holds

This should generate 6-8k trades with better win rate, targeting score of -0.35 to -0.40.
"""

from __future__ import annotations
from collections import deque
from typing import Optional, Tuple

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback_bars: int = 20,
        delta_ratio_threshold: float = 0.35,
        min_volume: int = 40,
        stop_offset: float = 0.75,
        target_offset: float = 5.0,
        size: int = 1,
        wick_min_ratio: float = 0.15,
        breakout_momentum_threshold: float = 0.20,
    ) -> None:
        self.interval = interval
        self.lookback_bars = lookback_bars
        self.delta_ratio_threshold = delta_ratio_threshold
        self.min_volume = min_volume
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.wick_min_ratio = wick_min_ratio
        self.breakout_momentum_threshold = breakout_momentum_threshold

        # Track bars and pending signals
        self.bars: deque = deque(maxlen=500)
        self.current_session: Optional[int] = None
        # State machine: None -> absorption_detected -> breakout_confirmed -> ready_to_enter
        self.absorption_state: Optional[Tuple[str, object, int]] = None  # (direction, absorption_bar, bars_since_breakout)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            self.absorption_state = None

        # Get current bar
        if self.interval not in ctx.bars:
            return

        current_bar = ctx.bars[self.interval]
        self.bars.append(current_bar)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Need minimum history
        if len(self.bars) < self.lookback_bars + 1:
            return

        # State machine: handle pending absorption states
        if self.absorption_state is not None:
            direction, absorption_bar, bars_since_breakout = self.absorption_state

            if bars_since_breakout == 0:
                # First bar after absorption: check for breakout
                if self._check_breakout(current_bar, direction, absorption_bar):
                    # Breakout confirmed, wait for persistence
                    self.absorption_state = (direction, absorption_bar, 1)
                    return
                else:
                    # No breakout, clear state
                    self.absorption_state = None
            else:
                # We've seen a breakout, check for persistence
                if self._check_persistence(current_bar, direction, absorption_bar):
                    # Move persists! Enter the trade
                    tag = f"absorption-{direction.lower()}-persistent"

                    if direction == "BUY":
                        ctx.buy(
                            size=self.size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag=tag,
                        )
                    else:
                        ctx.sell(
                            size=self.size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag=tag,
                        )

                    self.absorption_state = None
                    return
                elif bars_since_breakout >= 2:
                    # Move didn't persist long enough, clear state
                    self.absorption_state = None
                else:
                    # Keep waiting for persistence
                    self.absorption_state = (direction, absorption_bar, bars_since_breakout + 1)
                    return

        # Look for new absorption signals on this bar
        absorption_direction = self._detect_absorption(current_bar)
        if absorption_direction is not None:
            # Store as pending - will check for breakout on next bar
            self.absorption_state = (absorption_direction, current_bar, 0)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_absorption(self, current_bar) -> Optional[str]:
        """
        Detect absorption: strong one-way delta with price rejection and low momentum.

        Absorption characteristics:
        - High delta ratio (35%+ one-sided)
        - Meaningful wick in opposite direction (price rejected)
        - Low momentum (close near open, not extended move)
        - Sufficient volume

        Returns "BUY" for upside absorption, "SELL" for downside, or None.
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

        # Calculate momentum (how extended the close is from open)
        momentum_ratio = (
            abs(current_bar.close - current_bar.open) / bar_range
            if bar_range > 0
            else 0
        )

        # Upside absorption: positive delta with upper wick and low momentum
        if current_bar.delta > 0:
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0

            # Strong delta, meaningful wick (rejection), low momentum (no follow-through)
            # Close must be above open (bullish flow) despite the wick
            if (
                wick_ratio > self.wick_min_ratio
                and momentum_ratio < 0.50
                and current_bar.close > current_bar.open
            ):
                return "BUY"

        # Downside absorption: negative delta with lower wick and low momentum
        elif current_bar.delta < 0:
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0

            # Strong delta, meaningful wick (rejection), low momentum (no follow-through)
            # Close must be below open (bearish flow) despite the wick
            if (
                wick_ratio > self.wick_min_ratio
                and momentum_ratio < 0.50
                and current_bar.close < current_bar.open
            ):
                return "SELL"

        return None

    def _check_breakout(self, current_bar, direction: str, absorption_bar) -> bool:
        """
        Check if current bar breaks out past the absorption extreme.

        For BUY: close above absorption high with positive delta
        For SELL: close below absorption low with negative delta
        """
        bar_range = current_bar.high - current_bar.low
        if bar_range <= 0:
            return False

        momentum_ratio = (
            abs(current_bar.close - current_bar.open) / bar_range
            if bar_range > 0
            else 0
        )

        if direction == "BUY":
            # Breakout above absorption high with bullish momentum and delta
            if (
                current_bar.close > absorption_bar.high
                and current_bar.delta > 0
                and momentum_ratio > self.breakout_momentum_threshold
            ):
                return True

        else:  # SELL
            # Breakdown below absorption low with bearish momentum and delta
            if (
                current_bar.close < absorption_bar.low
                and current_bar.delta < 0
                and momentum_ratio > self.breakout_momentum_threshold
            ):
                return True

        return False

    def _check_persistence(self, current_bar, direction: str, absorption_bar) -> bool:
        """
        Check if the move persists in the same direction.

        For BUY: bar stays above absorption high, close above open
        For SELL: bar stays below absorption low, close below open
        """
        if direction == "BUY":
            # Must stay above absorption high and show bullish close
            if current_bar.low > absorption_bar.high and current_bar.close > current_bar.open:
                return True

        else:  # SELL
            # Must stay below absorption low and show bearish close
            if current_bar.high < absorption_bar.low and current_bar.close < current_bar.open:
                return True

        return False
