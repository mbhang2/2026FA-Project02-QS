"""
Volume profile absorption strategy - test directional flow persistence with simplified filters.

Analysis of trial history and byte-identical issue:

The persistent byte-identical trades across many trials (especially with confirmation
logic) suggest that the CONFIRMATION FILTER itself is not differentiating - the same
bars that pass absorption detection also happen to be followed by bars that confirm
on synthetic data.

Key insight from best performers:
- #221 (+0.1284, 434 trades): Confirmation approach
- #195 (+0.0646, 535 trades): Simple direct entry, no confirmation
- #165 (-0.0350, 658 trades): Straightforward absorption
- #81 (-0.0096, 622 trades): Clean, simple logic

The fact that #221 (confirmation) beat simpler approaches suggests confirmation
DOES work on real data. But on synthetic data, confirmation produces identical
trades, which means the synthetic data's bar sequences happen to confirm the same
absorption bars regardless.

New approach: Instead of next-bar confirmation, use a FLOW PERSISTENCE check on
the SAME bar. The hypothesis is: "Does absorption at a level precede a move away?"

This can be tested by looking at whether the absorption bar itself shows flow
that is CONSISTENT with the direction we expect to move:

1. For long (buy pressure): The bar should show sustained buying even though price
   was pushed down. This means: high volume AND buy_volume > sell_volume AND
   close is not at the extreme low (some recovery).

2. For short (sell pressure): The bar should show sustained selling even though
   price was pushed up. This means: high volume AND sell_volume > buy_volume AND
   close is not at the extreme high (some recovery).

This is a SAME-BAR filter that tests flow quality directly, not next-bar sequence.
It MUST produce different synthetic results because it depends on bar.buy_volume
and bar.sell_volume, which are different across bars.

Additionally, require that the bar's close be WITHIN the session's natural price
levels (0.25-point grid), indicating it settled at a significant level where
absorption matters.

Expected: 400-600 trades with clear synthetic differentiation from confirmation
approaches, targeting -0.0100 to +0.0500 based on flow persistence filtering.
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
        min_flow_ratio: float = 0.55,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.min_flow_ratio = min_flow_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

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
            self._entered_bar_timestamps = set()

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with flow persistence filter
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _is_at_natural_level(self, price: float) -> bool:
        """Check if price is at a natural 0.25-point level."""
        rounded = round(price * 4) / 4
        return abs(price - rounded) < 0.01

    def _detect_absorption(self, bar) -> Optional[dict]:
        """
        Detect absorption bar based on:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Significant wick showing price rejection (>= 0.50 points)
        3. Flow persistence: directional flow ratio >= threshold (0.55)
        4. Close positioned at natural level (0.25-point grid)
        5. Valid stop/target distance
        
        Flow persistence means:
        - For long: buy_volume / (buy_volume + sell_volume) >= 0.55
        - For short: sell_volume / (buy_volume + sell_volume) >= 0.55
        """
        if bar.volume == 0:
            return None

        delta = bar.delta
        delta_ratio = abs(delta) / bar.volume if bar.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 4: Close at natural level
        if not self._is_at_natural_level(bar.close):
            return None

        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return None

        # Calculate flow ratio (handling unknown_volume)
        attributed_volume = bar.buy_volume + bar.sell_volume
        if attributed_volume == 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = bar.close - bar.low
            
            # Filter 2: Lower wick must be substantial
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 3: Flow persistence - buy flow must dominate
            buy_ratio = bar.buy_volume / attributed_volume
            if buy_ratio < self.min_flow_ratio:
                return None
            
            # Calculate stop and target
            stop_price = bar.low - self.stop_buffer
            stop_distance = bar.close - stop_price
            
            # Filter 5: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = bar.high - bar.close
            
            # Filter 2: Upper wick must be substantial
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 3: Flow persistence - sell flow must dominate
            sell_ratio = bar.sell_volume / attributed_volume
            if sell_ratio < self.min_flow_ratio:
                return None
            
            # Calculate stop and target
            stop_price = bar.high + self.stop_buffer
            stop_distance = stop_price - bar.close
            
            # Filter 5: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

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
