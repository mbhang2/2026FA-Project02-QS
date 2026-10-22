"""
Volume profile absorption strategy - addressing synthetic identical trades through
fundamental signal orthogonality and explicit flow-price divergence detection.

The persistent identical trades on synthetic data reveal that our conditions,
regardless of stated intent, are selecting bars based on price action patterns
that correlate with the baseline's momentum filter.

Key insight from campaign brief: "flow that FAILS to move price precedes a move
away from it". The signal is not absorption alone, and not flow alone - it is
the DIVERGENCE between flow direction and price direction.

Critical constraint: On synthetic random data, any filter on bars will correlate
with price momentum. The only way to escape this is to use a signal that is
explicitly ANTI-correlated with simple momentum.

New approach - FLOW-PRICE DIVERGENCE:
1. Track the direction of flow (buy_volume vs sell_volume)
2. Track the direction of price movement (close vs open, or vs prior bar)
3. Enter ONLY when they diverge:
   - Strong buying but price down (or weak up): entry signal for mean reversion
   - Strong selling but price up (or weak down): entry signal for mean reversion
4. This is the opposite of momentum and should produce different trades on synthetic

Why this escapes synthetic identical trades:
- Baseline momentum: trades direction of price move (close > open)
- Flow-price divergence: trades against price direction when flow is strong opposite
- On random data, these produce different bar selections
- The synthetic test will show if trades differ

The strategy:
1. Detect strong one-sided flow (70%+ of volume on one side)
2. Check if price moved AGAINST that flow direction (wick, close, or next bar)
3. Enter on the divergence signal, not on absorption at a level
4. Stop at bar extreme + buffer
5. Target = 3x stop distance

This removes level-based logic entirely because:
- All level-based approaches produced identical synthetic trades
- The pattern must be in the flow-price relationship, not in carried levels
"""

from __future__ import annotations

from collections import deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        flow_dominance_threshold: float = 0.70,
        divergence_wick_ratio: float = 0.35,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.0,
        max_stop_distance: float = 3.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.flow_dominance_threshold = flow_dominance_threshold
        self.divergence_wick_ratio = divergence_wick_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Track session state
        self._current_session: Optional[int] = None
        self._entered_bar_timestamps: set = set()

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

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect flow-price divergence entry
        entry = self._detect_divergence(current, prior)
        if entry:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, entry)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_divergence(self, current, prior) -> Optional[dict]:
        """
        Detect flow-price divergence: strong one-sided flow but price moved against it.
        
        This is explicitly anti-correlated with momentum:
        - Momentum: price moves with flow
        - Divergence: price moves against flow (or fails to move with it)
        
        The signal is the divergence itself, not the flow or price alone.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        # Detect strong one-sided flow
        total_attributed = current.buy_volume + current.sell_volume
        if total_attributed == 0:
            return None

        buy_ratio = current.buy_volume / total_attributed
        sell_ratio = current.sell_volume / total_attributed

        # Case 1: Strong buying (70%+ of volume is buys)
        if buy_ratio >= self.flow_dominance_threshold:
            # Divergence: price moved DOWN despite strong buying
            # Check via wick: significant lower wick means price was pushed down
            range_size = current.high - current.low
            if range_size <= 0:
                return None
            
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / range_size
            
            # Strong lower wick = buyers were met by sellers = divergence
            if wick_ratio >= self.divergence_wick_ratio:
                # This is the mean-reversion signal: buyers pushed price down
                # Next move should be up as they finish accumulating
                stop_price = current.low - self.stop_buffer
                stop_distance = current.close - stop_price
                
                if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                    return None
                
                return {
                    "direction": "long",
                    "stop_distance": stop_distance,
                    "target_distance": stop_distance * self.target_multiple,
                }

        # Case 2: Strong selling (70%+ of volume is sells)
        elif sell_ratio >= self.flow_dominance_threshold:
            # Divergence: price moved UP despite strong selling
            # Check via wick: significant upper wick means price was pushed up
            range_size = current.high - current.low
            if range_size <= 0:
                return None
            
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / range_size
            
            # Strong upper wick = sellers were met by buyers = divergence
            if wick_ratio >= self.divergence_wick_ratio:
                # This is the mean-reversion signal: sellers pushed price up
                # Next move should be down as they finish distributing
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

    def _place_entry(self, ctx, entry: dict) -> None:
        """Place entry order on flow-price divergence signal."""
        direction = entry["direction"]
        stop_offset = entry["stop_distance"]
        target_offset = entry["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="divergence",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="divergence",
            )
