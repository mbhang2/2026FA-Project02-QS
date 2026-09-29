"""
Intraday momentum strategy with ultra-high conviction entry filtering.

Analysis of prior trials:
- Trial #0: -0.7386, 39,797 trades (baseline)
- Trial #5: -0.6642, 24,270 trades (best, 10% improvement)
- Trial #6: -0.7694, 15,966 trades (worse - too few trades, over-filtered)

Key insight: Trial #5 achieved the best score with moderate trade count.
Trial #6 over-filtered and made things worse. The sweet spot appears to be
around 20-25k trades per 384 sessions (~52-65 trades/session).

The commission cost is brutal: 0.2 points round-trip per trade means we need
positive expectancy of >0.2 points per trade just to break even. At -0.66 score
on 24k trades, we're paying ~$1,900 in commission per session.

New hypothesis - maximize signal quality without over-filtering:
1. Keep the percentile-based range filter but use 35% (slightly lower threshold)
   to capture more high-conviction moves without noise
2. Require BOTH bars to show strong directional closes (>65% of range)
3. Add a volatility regime filter: only trade when recent volatility is
   elevated (top 30% of session ranges) - avoids low-vol choppy markets
4. Use asymmetric brackets: tighter stop (0.30) to cut losses quickly,
   modest target (0.80) to capture the continuation
5. Hold for 2 bars maximum to catch the momentum but exit before reversal
6. Add a "strength confirmation": current bar's momentum must exceed prior bar's
   momentum (close - open difference)

The refined momentum hypothesis: a bar that:
- Has range in top 35% of recent session ranges (strong volatility)
- Session is in elevated volatility regime (top 30% of ranges)
- Current bar closes >65% decisively in one direction
- Prior bar also closed >55% decisively in same direction
- Current bar's close-to-open move exceeds prior bar's move (momentum increasing)
- Indicates strong continuation within 2 bars

This should maintain ~20-25k trades while improving signal quality.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 12,
        range_percentile: float = 0.35,
        volatility_percentile: float = 0.30,
        close_strength: float = 0.65,
        prior_close_strength: float = 0.55,
        hold_bars: int = 2,
        stop_offset: float = 0.30,
        target_offset: float = 0.80,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.range_percentile = range_percentile
        self.volatility_percentile = volatility_percentile
        self.close_strength = close_strength
        self.prior_close_strength = prior_close_strength
        self.hold_bars = hold_bars
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size

        # State tracking across callbacks
        self._session: int | None = None
        self._bars_held: int = 0
        self._ranges: deque[float] = deque(maxlen=lookback + 1)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset on session change
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_held = 0
            self._ranges.clear()

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        current_bar = history[-1]
        current_range = current_bar.high - current_bar.low
        self._ranges.append(current_range)

        # Manage existing position
        if ctx.position:
            self._bars_held += 1
            if self._bars_held >= self.hold_bars:
                ctx.close()
                self._bars_held = 0
            return

        # Not in position; check for entry
        if ctx.working_order_ids:
            return

        # Need sufficient history
        if len(self._ranges) < self.lookback or len(history) < 2:
            return

        # Compute range thresholds
        ranges_list = list(self._ranges)
        sorted_ranges = sorted(ranges_list)
        
        # Percentile for exceptional moves (top 35%)
        range_percentile_idx = max(0, int(len(sorted_ranges) * self.range_percentile))
        threshold_range = sorted_ranges[range_percentile_idx]
        
        # Percentile for volatility regime (top 30%)
        vol_percentile_idx = max(0, int(len(sorted_ranges) * self.volatility_percentile))
        vol_threshold = sorted_ranges[vol_percentile_idx]

        # Skip if session volatility is not elevated
        recent_vol = sorted_ranges[-1] if len(sorted_ranges) > 0 else 0
        if recent_vol < vol_threshold:
            return

        # Skip if current bar's range is not exceptional
        if current_range < threshold_range:
            return

        # Check if bar closes decisively in one direction
        if current_range > 0:
            close_position_ratio = (current_bar.close - current_bar.low) / current_range
        else:
            return

        prev_bar = history[-2]
        prev_range = prev_bar.high - prev_bar.low
        
        # Calculate momentum: close-to-open change for both bars
        current_momentum = current_bar.close - current_bar.open
        prior_momentum = prev_bar.close - prev_bar.open

        # Strong upside close: close is in upper 65% of the range
        if close_position_ratio > self.close_strength:
            # Confirm with momentum
            if prev_range > 0:
                prev_close_ratio = (prev_bar.close - prev_bar.low) / prev_range
                # Require prior bar to also be in upper portion (moving up)
                # and current momentum exceeds prior momentum (accelerating)
                if (prev_close_ratio > self.prior_close_strength and 
                    prev_bar.close > prev_bar.open and
                    current_momentum > prior_momentum and
                    current_momentum > 0):
                    order_id = ctx.buy(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="momentum-long",
                    )
                    if order_id is not None:
                        self._bars_held = 0

        # Strong downside close: close is in lower 35% of the range
        elif close_position_ratio < (1.0 - self.close_strength):
            # Confirm with momentum
            if prev_range > 0:
                prev_close_ratio = (prev_bar.close - prev_bar.low) / prev_range
                # Require prior bar to also be in lower portion (moving down)
                # and current momentum exceeds prior momentum (accelerating down)
                if (prev_close_ratio < (1.0 - self.prior_close_strength) and 
                    prev_bar.close < prev_bar.open and
                    current_momentum < prior_momentum and
                    current_momentum < 0):
                    order_id = ctx.sell(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="momentum-short",
                    )
                    if order_id is not None:
                        self._bars_held = 0

    def on_fill(self, ctx) -> None:
        # Reset hold counter on fill
        self._bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        # Handle order lifecycle events
        if ctx.event.kind in ("order_rejected", "day_expired", "gtd_expired", "cancel_confirmed"):
            self._bars_held = 0
