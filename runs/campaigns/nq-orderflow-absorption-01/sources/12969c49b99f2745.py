"""
Absorption strategy: simplified entry logic with better confirmation.

Analysis of prior trials shows the best score (-0.5016) came from moderate
frequency with clear absorption + confirmation patterns. The current strategy
uses a 2-bar lookback which works, but the confirmation logic may be too strict.

Key insight from trial history:
- #17 (best: -0.5016) used simple absorption detection + immediate entry
- #23+ attempts to add confirmation windows made things worse
- The synthetic data shows identical trades to #17, suggesting the refinements
  aren't changing behavior on simple data
- This means the confirmation logic is either redundant or broken

The real problem: We're overthinking confirmation.
- An absorption bar (strong delta + wick + low momentum) IS the signal
- The next bar doesn't need to confirm - we should enter ON absorption
- But we need to filter WHICH absorptions to trade
- Current filter (momentum < 0.60) may be too loose

New approach - back to basics with better filtering:
1. Detect absorption: strong delta (60%+) + meaningful wick + bar range check
2. Filter by volatility context: only trade if bar is within recent range band
3. Filter by delta conviction: delta ratio must be 60%+ (stricter than 50%)
4. Enter immediately on absorption (don't wait for confirmation bar)
5. Use fixed targets (5.0 points) that survive costs at this frequency
6. Structural stop at absorption extreme + 0.5 buffer
7. This should generate 15-25 trades per session with high conviction

The hypothesis: The best absorption signals are those with:
- Extreme delta ratio (60%+ of volume is one-sided, not just 50%)
- Clear wick rejection (18%+ of range)
- Bar size in normal range (not anomalously small or large)
- No need to wait for confirmation - absorption itself IS the signal

Differences from current:
- Remove 2-bar lookback complexity, go back to single-bar absorption detection
- Stricter delta ratio (60% instead of 50%)
- Add volatility band filter (bar range should be 40-150% of recent avg)
- Remove confirmation check entirely
- Simpler, more direct entry logic
"""

from __future__ import annotations
from collections import deque
from typing import Optional

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback_bars: int = 30,
        min_delta_ratio: float = 0.60,
        min_volume: int = 60,
        stop_offset: float = 0.75,
        target_offset: float = 5.0,
        size: int = 1,
        wick_min_ratio: float = 0.18,
        volatility_lookback: int = 20,
        range_min_ratio: float = 0.40,
        range_max_ratio: float = 1.50,
    ) -> None:
        self.interval = interval
        self.lookback_bars = lookback_bars
        self.min_delta_ratio = min_delta_ratio
        self.min_volume = min_volume
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.wick_min_ratio = wick_min_ratio
        self.volatility_lookback = volatility_lookback
        self.range_min_ratio = range_min_ratio
        self.range_max_ratio = range_max_ratio

        # Track bars
        self.bars: deque = deque(maxlen=500)
        self.current_session: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session

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

        # Check if current bar is absorption
        direction = self._is_absorption(current_bar)
        if direction is None:
            return

        # Execute trade
        if direction == Side.BUY:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _is_absorption(self, bar) -> Optional[Side]:
        """
        Check if a bar shows absorption characteristics.
        
        Absorption shows:
        1. Strong delta ratio (60%+ of volume is one-sided)
        2. Meaningful wick in opposite direction (price rejection)
        3. Bar range is within normal volatility band
        4. Sufficient volume
        
        Returns Side.BUY (upside absorption), Side.SELL (downside), or None.
        """
        if bar.volume < self.min_volume:
            return None

        # Calculate delta ratio
        delta_ratio = (
            abs(bar.delta) / bar.volume
            if bar.volume > 0
            else 0
        )

        # Need strong one-way flow for absorption
        if delta_ratio < self.min_delta_ratio:
            return None

        bar_range = bar.high - bar.low
        if bar_range < 0.10:
            return None

        # Check that bar range is within normal volatility band
        recent_bars = list(self.bars)[-self.volatility_lookback :]
        if recent_bars:
            avg_range = sum(b.high - b.low for b in recent_bars) / len(recent_bars)
        else:
            avg_range = bar_range

        # Bar should be within 40-150% of recent average range
        if avg_range > 0:
            range_ratio = bar_range / avg_range
            if range_ratio < self.range_min_ratio or range_ratio > self.range_max_ratio:
                return None

        # Upside absorption: positive delta with upper wick
        if bar.delta > 0:
            upper_wick = bar.high - max(bar.open, bar.close)
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick (price was rejected upside)
            if wick_ratio > self.wick_min_ratio:
                # Close should be above open for bullish absorption
                if bar.close > bar.open:
                    return Side.BUY

        # Downside absorption: negative delta with lower wick
        elif bar.delta < 0:
            lower_wick = min(bar.open, bar.close) - bar.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick (price was rejected downside)
            if wick_ratio > self.wick_min_ratio:
                # Close should be below open for bearish absorption
                if bar.close < bar.open:
                    return Side.SELL

        return None
