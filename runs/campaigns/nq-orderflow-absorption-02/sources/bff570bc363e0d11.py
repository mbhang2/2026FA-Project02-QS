"""
Volume profile absorption strategy - refined for consistent exposure.

Analysis of prior attempts:
1. Trials #0-#7: 0-2 sessions exposed (floor is 192+ sessions / 50%)
2. Synthetic test: identical trades to baseline - changes aren't manifesting
3. Core issue: absorption detection is too strict, rejection logic inverted
4. Prior crash #0: stop placement had sign error for shorts (fixed in #7)
5. Current attempt: stop placement logic appears correct but still no exposure

Key refinement in this version:
- Absorption rejection logic was backwards: strong delta WITH price moving in that
  direction is normal, not absorption. Absorption is strong delta with FAILURE.
- For long (buy delta): absorption is when price closes DOWN despite buying
- For short (sell delta): absorption is when price closes UP despite selling
- Previous versions had this right conceptually but the conditional logic was
  checking the wrong comparisons
- Simplify: if delta_ratio > 0.50, check if price failed to extend in that direction
- Remove multi-bar confirmation requirement - act on absorption immediately
- Widen acceptance criteria: any moderate delta + rejection is tradeable
- Keep stop structural (at absorption bar extreme + buffer) and target at multiple
"""

from __future__ import annotations

from collections import deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        min_stop_distance: float = 0.5,
        max_stop_distance: float = 4.0,
        size: int = 1,
        bars_lookback: int = 20,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.min_stop_distance = min_stop_distance
        self.max_stop_distance = max_stop_distance
        self.size = size
        self.bars_lookback = bars_lookback

        # Session tracking
        self._current_session: Optional[int] = None
        self._bars_in_session: list = []
        
        # Track recent bars for context
        self._recent_bars: deque = deque(maxlen=bars_lookback)
        
        # Track what we've acted on to avoid duplicates in same bar
        self._last_absorption_ts: Optional[int] = None

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
            self._on_session_change(current)
            self._current_session = current.session

        # Accumulate bars
        self._bars_in_session.append(current)
        self._recent_bars.append(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Check for absorption setup
        if len(history) >= 2:
            absorption = self._detect_absorption(current, history[-2])
            if absorption:
                # Only place if we haven't acted on this exact timestamp
                if self._last_absorption_ts != current.close_ts:
                    self._place_entry(ctx, absorption)
                    self._last_absorption_ts = current.close_ts

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, bar) -> None:
        """Reset per-session state."""
        self._bars_in_session = []
        self._last_absorption_ts = None

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect absorption: strong one-way delta where price FAILS to extend.
        
        Key insight: absorption is not just strong delta, it's strong delta
        that doesn't move price in the direction of the flow.
        
        - Long absorption (buy delta): strong buying but price closes DOWN or flat
        - Short absorption (sell delta): strong selling but price closes UP or flat
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Need moderate to strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Detect absorption based on delta direction vs price movement
        if delta > 0:
            # Buying pressure: absorption occurs when price fails to move up
            # or moves down despite the buying
            
            # Check if price closed below or at the prior bar's close
            # This indicates buying pressure being absorbed/rejected
            if current.close <= prior.close:
                # Strong buying but price didn't go up - absorption signal
                stop_price = current.low - self.stop_buffer
                stop_distance = current.close - stop_price
                
                if self.min_stop_distance <= stop_distance <= self.max_stop_distance:
                    return {
                        "direction": "long",
                        "stop_distance": stop_distance,
                        "target_distance": stop_distance * self.target_multiple,
                    }

        elif delta < 0:
            # Selling pressure: absorption occurs when price fails to move down
            # or moves up despite the selling
            
            # Check if price closed above or at the prior bar's close
            # This indicates selling pressure being absorbed/rejected
            if current.close >= prior.close:
                # Strong selling but price didn't go down - absorption signal
                stop_price = current.high + self.stop_buffer
                stop_distance = stop_price - current.close
                
                if self.min_stop_distance <= stop_distance <= self.max_stop_distance:
                    return {
                        "direction": "short",
                        "stop_distance": stop_distance,
                        "target_distance": stop_distance * self.target_multiple,
                    }

        return None

    def _place_entry(self, ctx, absorption: dict) -> None:
        """Place entry with structural stop and target."""
        direction = absorption["direction"]
        stop_offset = absorption["stop_distance"]
        target_offset = absorption["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption-only",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption-only",
            )
