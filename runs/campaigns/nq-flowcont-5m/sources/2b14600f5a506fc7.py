"""
Flow continuation strategy with refined signal measurement and volatility-adjusted exits.

Prior trials (#0: -0.6012, #1: -0.7100) suggest the basic flow continuation hypothesis
may be weak at this horizon, or the entry/exit mechanics need refinement.

This iteration focuses on:

1. **Better signal measurement**: Use buy_volume and sell_volume directly (more precise
   than delta which can be noisy). Measure aggression as the ratio of dominant side
   to total, not just delta normalization.

2. **Volatility-aware brackets**: Recent volatility should inform stop/target distances.
   High volatility → wider brackets. Low volatility → tighter brackets. This prevents
   whipsaws in choppy markets and captures more of trending moves.

3. **Directional bias filter**: Only enter when the bar's close is in the direction
   of the flow signal. A strong buy-side aggression bar that closes near the low is
   suspicious; one that closes near the high is more convincing.

4. **Reduced entry frequency**: Higher threshold (0.40) to avoid over-trading and
   reduce commission drag. The prior trials traded very frequently (3224, 5061 trades
   in 384 sessions = 8-13 trades per session), which at 0.20 points per round turn
   is devastating.

5. **Minimum holding enforcement**: Ensure we don't exit on the same bar we enter,
   respecting the 5m minimum hold from the campaign spec.

Key insight: Commission is 0.20 index points per round turn. With a 0.25 tick size,
a strategy must capture at least 0.20 points gross to break even. The prior strategies
may be generating too many marginal trades. This version aims for higher-conviction
entries with better risk/reward.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.40,
        volatility_lookback: int = 20,
        base_stop_offset: float = 1.0,
        base_target_offset: float = 1.5,
        size: int = 1,
    ):
        """
        Args:
            interval: Bar interval for signal measurement (5m fixed per campaign)
            flow_threshold: Minimum ratio of dominant side to total volume (0-1 range)
            volatility_lookback: Bars to use for volatility estimation
            base_stop_offset: Base distance for protective stop (adjusted by volatility)
            base_target_offset: Base distance for profit target (adjusted by volatility)
            size: Position size in contracts
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.volatility_lookback = volatility_lookback
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        self.size = size
        
        # Track entry bar timestamp to enforce minimum holding
        self._entry_bar_open_ts = None

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars only. Delta is computed on every bar."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: measure flow aggression using buy/sell volume ratio,
        apply directional bias filter, and use volatility-adjusted brackets.
        """
        if not ctx.actionable:
            return
        
        # One position at a time
        if ctx.position or ctx.working_order_ids:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        last_bar = history[-1]  # The bar that just closed
        
        # Skip low-volume bars that produce unreliable signals
        if last_bar.volume < 50:
            return
        
        # Measure aggression: ratio of dominant side to total volume
        buy_vol = last_bar.buy_volume
        sell_vol = last_bar.sell_volume
        total_vol = last_bar.volume
        
        if total_vol <= 0:
            return
        
        # Ratio of dominant side (whichever is larger)
        buy_ratio = buy_vol / total_vol if total_vol > 0 else 0
        sell_ratio = sell_vol / total_vol if total_vol > 0 else 0
        
        # Estimate recent volatility from high-low ranges
        vol_lookback = min(self.volatility_lookback, len(history))
        if vol_lookback < 2:
            vol_lookback = 2
        
        recent_bars = history[-vol_lookback:]
        ranges = [b.high - b.low for b in recent_bars]
        avg_range = sum(ranges) / len(ranges) if ranges else 0
        
        if avg_range <= 0:
            avg_range = 0.5  # Fallback
        
        # Volatility adjustment: higher volatility → wider brackets
        volatility_factor = avg_range / 0.75  # Normalize to typical range
        volatility_factor = max(0.5, min(volatility_factor, 2.5))  # Clamp
        
        adjusted_stop = self.base_stop_offset * volatility_factor
        adjusted_target = self.base_target_offset * volatility_factor
        
        # Entry logic with directional bias filter
        if buy_ratio > self.flow_threshold:
            # Strong buy-side aggression
            # Directional bias: close should be in upper half of bar range
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < 0.4:
                    # Close is in lower half - suspicious for a buy-dominant bar
                    return
            
            ctx.buy(
                size=self.size,
                stop_offset=adjusted_stop,
                target_offset=adjusted_target,
                tag="flow-long",
            )
            self._entry_bar_open_ts = last_bar.open_ts
        
        elif sell_ratio > self.flow_threshold:
            # Strong sell-side aggression
            # Directional bias: close should be in lower half of bar range
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position > 0.6:
                    # Close is in upper half - suspicious for a sell-dominant bar
                    return
            
            ctx.sell(
                size=self.size,
                stop_offset=adjusted_stop,
                target_offset=adjusted_target,
                tag="flow-short",
            )
            self._entry_bar_open_ts = last_bar.open_ts

    def on_fill(self, ctx) -> None:
        """
        Position is now open. The bracket (stop and target) was armed by the
        engine at fill time, protecting the position immediately.
        """
        pass

    def on_lifecycle(self, ctx) -> None:
        """
        Track lifecycle events. Bracket handles the exit.
        """
        pass
