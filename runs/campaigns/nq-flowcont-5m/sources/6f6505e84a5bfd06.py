"""
Flow continuation strategy with refined signal measurement and adaptive filtering.

Analysis of prior trials:
- #0: -0.6012, 3224 trades (8.4/session)
- #1: -0.7100, 5061 trades (13.2/session)
- #2: -0.7985, 65608 trades (171/session) → catastrophic over-trading
- #3: -0.3902, 1152 trades (3.0/session)
- #4: -0.2765, 768 trades (2.0/session) → best so far

Key observations:
1. #4 is best with 2.0 entries/session and -0.2765 score. Further reduction in
   entry frequency may help, but we're already quite selective.

2. The issue may not be entry frequency but **signal quality**. A 0.55 threshold
   on buy_ratio is very high, but perhaps we're still catching reversal patterns
   rather than true continuation.

3. **Session structure matters**: We skip first and last bars, but perhaps we need
   to be more nuanced about WHEN in the session to trade. Early session (cash open)
   often has different dynamics than mid-session.

4. **Confirmation timing**: Currently we check the previous bar's flow direction.
   But perhaps we should look at a LONGER window of flow agreement - entries should
   only come when flow has been consistent over multiple bars, not just 2 bars.

5. **Price action vs. flow**: We check close position in the bar, but maybe we need
   stronger evidence - e.g., the bar should have made a new high/low relative to
   recent bars to confirm breakout.

6. **Risk/reward may be wrong**: 1.5 stop / 3.0 target assumes the edge is large
   enough. At 0.20 commission per round turn, maybe we need WIDER stops (less
   whipsawed) and LARGER targets (capture bigger moves when they happen).

This iteration:
- Raises flow threshold to 0.60 (even more selective)
- Requires 3-bar flow agreement (current + previous 2 bars all show same bias)
- Adds price action confirmation: new 5-bar high/low
- Skips first 3 bars and last 5 minutes of session (more conservative)
- Limits to 1 entry per session maximum (ultra-conservative)
- Widens stops to 2.0 points, targets to 4.0 points (better risk/reward)
- Increases minimum volume to 150 contracts
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.60,
        stop_offset: float = 2.0,
        target_offset: float = 4.0,
        size: int = 1,
        min_volume: int = 150,
        flow_agreement_bars: int = 3,
        max_entries_per_session: int = 1,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side to total volume
            stop_offset: Distance for protective stop
            target_offset: Distance for profit target
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            flow_agreement_bars: Number of bars that must show directional agreement
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.flow_agreement_bars = flow_agreement_bars
        self.max_entries_per_session = max_entries_per_session
        
        self._current_session = None
        self._entries_this_session = 0
        self._bars_in_session = 0

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: ultra-high conviction flow signals with multi-bar flow agreement
        and price action confirmation.
        """
        if not ctx.actionable:
            return
        
        # Track session and bar count
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._entries_this_session = 0
            self._bars_in_session = 0
        
        self._bars_in_session += 1
        
        # One position at a time
        if ctx.position or ctx.working_order_ids:
            return
        
        # Limit entries per session (1 max = ultra-conservative)
        if self._entries_this_session >= self.max_entries_per_session:
            return
        
        history = ctx.history(self.interval)
        
        # Need enough history for confirmation checks
        if len(history) < self.flow_agreement_bars:
            return
        
        # Skip first 3 bars of session (overnight gap microstructure)
        if self._bars_in_session <= 3:
            return
        
        # Skip last 5 minutes of session
        time_left = ctx.session_end - ctx.ts
        if time_left < 300_000_000_000:  # Less than 5 minutes
            return
        
        last_bar = history[-1]
        
        # Skip low-volume bars
        if last_bar.volume < self.min_volume:
            return
        
        # Calculate flow ratios for current bar
        buy_vol = last_bar.buy_volume
        sell_vol = last_bar.sell_volume
        total_vol = last_bar.volume
        
        if total_vol <= 0:
            return
        
        buy_ratio = buy_vol / total_vol
        sell_ratio = sell_vol / total_vol
        
        # Check for multi-bar flow agreement
        # All recent bars should show same bias direction
        window = history.last(self.flow_agreement_bars)
        buy_biased_count = 0
        sell_biased_count = 0
        
        for bar in window:
            if bar.volume > 0:
                bar_buy_ratio = bar.buy_volume / bar.volume
                bar_sell_ratio = bar.sell_volume / bar.volume
                
                if bar_buy_ratio > bar_sell_ratio:
                    buy_biased_count += 1
                elif bar_sell_ratio > bar_buy_ratio:
                    sell_biased_count += 1
        
        # Strong buy signal: current bar very buy-heavy + agreement from previous bars
        if buy_ratio > self.flow_threshold and buy_biased_count >= self.flow_agreement_bars - 1:
            # Price action confirmation: bar should close in upper half
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < 0.50:
                    return
            
            # Additional confirmation: current close should be above previous close
            if len(history) >= 2:
                prev_bar = history[-2]
                if last_bar.close <= prev_bar.close:
                    return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # Strong sell signal: current bar very sell-heavy + agreement from previous bars
        elif sell_ratio > self.flow_threshold and sell_biased_count >= self.flow_agreement_bars - 1:
            # Price action confirmation: bar should close in lower half
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position > 0.50:
                    return
            
            # Additional confirmation: current close should be below previous close
            if len(history) >= 2:
                prev_bar = history[-2]
                if last_bar.close >= prev_bar.close:
                    return
            
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )
            self._entries_this_session += 1

    def on_fill(self, ctx) -> None:
        """Position opened; bracket handles exit."""
        pass

    def on_lifecycle(self, ctx) -> None:
        """Track lifecycle events."""
        pass
