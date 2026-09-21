"""
Flow continuation strategy with adaptive threshold and volatility-adjusted risk.

Analysis of prior trials:
- #4: -0.2765, 768 trades (2.0/session) → best so far
- #5: -0.2961, 384 trades (1.0/session) → too restrictive
- #6: -0.2427, 768 trades (2.0/session) → improvement on #4

Key insight from #6: Slight improvement over #4 suggests the refinements help.
But we're still losing money overall. The issue may be:

1. **Threshold is still too low**: 0.55 flow ratio catches some reversal patterns.
   The campaign note emphasized that flow continuation is NOT obvious - we may
   need even higher conviction (0.60+) to avoid whipsaws.

2. **Risk/reward ratio**: 1.5 stop / 3.0 target = 2.0 ratio. At 0.20 commission
   per round turn, we need the edge to be real. Maybe we need WIDER stops
   (less whipsawed by noise) and TIGHTER targets (take profits faster before
   reversal).

3. **Volatility adjustment**: All bars are not equal. High volatility sessions
   need wider stops; low volatility can use tighter ones. Current approach is
   static.

4. **Price action is weak**: Checking close position in bar is helpful but maybe
   not enough. We should also verify the bar actually MADE a move in the
   direction of the signal, not just closed there.

5. **Session timing**: Maybe we're too aggressive early in session. Cash open
   (first 30 min) has different dynamics. Trade more in mid-session.

6. **Flow ratio should be extreme**: Instead of 0.55, try 0.58-0.62. The sweet
   spot for high conviction without over-filtering might be higher.

This iteration:
- Raises flow threshold to 0.58 (stronger conviction, fewer false signals)
- Uses recent volatility to adjust stop/target sizes dynamically
- Requires bar to have made a directional move (high/low relative to recent bars)
- Skips first 3 bars (cash open microstructure is different)
- Increases minimum volume to 120 contracts (avoid thin bars where ratio is noisy)
- Keeps 2 entries per session (sweet spot from #4/#6)
- Adjusts risk/reward: 1.2 base stop / 2.5 base target, scaled by volatility
- Adds check: previous bar should be NEUTRAL or same direction, not opposite

The hypothesis: Flow continuation is real but ONLY when conviction is high.
We've been catching reversals by being too permissive. Higher threshold +
volatility-aware sizing + better price action confirmation should improve.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.58,
        base_stop_offset: float = 1.2,
        base_target_offset: float = 2.5,
        size: int = 1,
        min_volume: int = 120,
        max_entries_per_session: int = 2,
        volatility_window: int = 5,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side to total volume
                           0.58 = 58%+ of volume on one side (high conviction)
            base_stop_offset: Base distance for protective stop
            base_target_offset: Base distance for profit target
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
            volatility_window: Bars to look back for volatility calculation
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        self.volatility_window = volatility_window
        
        # Track entries per session
        self._current_session = None
        self._entries_this_session = 0
        self._bars_in_session = 0

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def _calculate_volatility(self, history):
        """
        Calculate recent volatility as average true range over recent bars.
        Returns a scaling factor: 1.0 = normal, >1.0 = high volatility.
        """
        if len(history) < self.volatility_window:
            return 1.0
        
        recent = history.last(self.volatility_window)
        ranges = []
        for bar in recent:
            ranges.append(bar.high - bar.low)
        
        avg_range = sum(ranges) / len(ranges) if ranges else 1.0
        
        # Volatility factor: if current volatility is 2x the average, scale by 1.5
        if avg_range > 0:
            volatility_factor = 1.0 + (avg_range - sum(ranges) / (2 * len(ranges))) / avg_range
            volatility_factor = max(0.8, min(1.5, volatility_factor))  # Clamp between 0.8 and 1.5
            return volatility_factor
        
        return 1.0

    def on_bars(self, ctx) -> None:
        """
        Entry logic: high-conviction flow signals with volatility-adjusted sizing
        and directional price action confirmation.
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
        
        # Limit entries per session
        if self._entries_this_session >= self.max_entries_per_session:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 2:
            return
        
        last_bar = history[-1]  # Bar that just closed
        
        # Skip first 3 bars of session (cash open has different microstructure)
        if self._bars_in_session <= 3:
            return
        
        # Skip last 5 minutes of session (end-of-day pressure)
        time_left = ctx.session_end - ctx.ts
        if time_left < 300_000_000_000:  # Less than 5 minutes (in nanoseconds)
            return
        
        # Skip low-volume bars (flow ratio unreliable on thin bars)
        if last_bar.volume < self.min_volume:
            return
        
        # Measure aggression: ratio of dominant side to total volume
        buy_vol = last_bar.buy_volume
        sell_vol = last_bar.sell_volume
        total_vol = last_bar.volume
        
        if total_vol <= 0:
            return
        
        buy_ratio = buy_vol / total_vol
        sell_ratio = sell_vol / total_vol
        
        # Get previous bar for directional confirmation
        prev_bar = history[-2]
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume if prev_bar.volume > 0 else 0.5
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume if prev_bar.volume > 0 else 0.5
        
        # Calculate volatility-adjusted stop/target sizes
        volatility_factor = self._calculate_volatility(history)
        stop_offset = self.base_stop_offset * volatility_factor
        target_offset = self.base_target_offset * volatility_factor
        
        # Strong buy signal: current bar has 58%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Directional confirmation: previous bar should NOT be strongly sell-biased
            if prev_sell_ratio > 0.58:
                return
            
            # Price action confirmation 1: close should be in upper half of bar
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < 0.50:
                    return
            
            # Price action confirmation 2: bar should have made a high move
            # (not just closed high on a narrow range)
            if len(history) >= 3:
                prior_bars = history.last(3)[:-1]  # Previous 2 bars
                prior_high = max(b.high for b in prior_bars)
                if last_bar.high <= prior_high:
                    return
            
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # Strong sell signal: current bar has 58%+ sell volume
        elif sell_ratio > self.flow_threshold:
            # Directional confirmation: previous bar should NOT be strongly buy-biased
            if prev_buy_ratio > 0.58:
                return
            
            # Price action confirmation 1: close should be in lower half of bar
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position > 0.50:
                    return
            
            # Price action confirmation 2: bar should have made a low move
            if len(history) >= 3:
                prior_bars = history.last(3)[:-1]  # Previous 2 bars
                prior_low = min(b.low for b in prior_bars)
                if last_bar.low >= prior_low:
                    return
            
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="flow-short",
            )
            self._entries_this_session += 1

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
