"""
Flow continuation strategy: refined entry confirmation with session-aware position limits.

Analysis of recent trials:
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → best baseline
- #19: -0.1631, 382 trades (0.99/session) → fewer trades competitive
- #27: -0.6699, 2 trades (0.005/session) → volatility range too restrictive
- #26: -0.3158, 1152 trades (3.0/session) → dynamic risk-reward hurt
- #25: -0.2991, 767 trades (2.0/session) → dual-threshold failed

Core insight from #27's failure:
The volatility filter was TOO restrictive (0.5-2.0 range), killing almost all trades.
This confirms: #9's simplicity is the real edge. The 0.55 threshold works.

New hypothesis - CONFIRMATION-BASED ENTRY WITH RELAXED SESSION LIMITS:
Instead of filtering by volatility (which killed trades), use a simpler confirmation:
1. Current bar shows strong flow (0.55+)
2. Previous bar shows agreement (not opposed at 0.55+)
3. Price action: close in correct half
4. ADDED: Require the bar to have ABOVE-AVERAGE volume for the session
   (this is a soft filter - we only skip very thin bars, not a hard range)

Why this works better than #27:
- #27 killed 99% of trades with hard volatility bounds (0.5-2.0)
- This approach uses RELATIVE volume (above session average) which is softer
- We keep most trades but filter out the thinnest, least reliable ones
- No hard cutoffs, just preference for thicker bars

Key changes from #9:
1. Track session average volume (rolling average of bars in current session)
2. Only enter if last_bar.volume > session_avg_volume * 0.9
   (require at least 90% of session average - very permissive)
3. Increase max_entries_per_session from 2 to 3
   (we'll naturally get fewer due to volume filter, but allow more attempts)
4. Keep everything else: 0.55 threshold, stop 1.5, target 3.0, session timing

Expected outcome:
- 700-800 trades (similar to #9, maybe slightly fewer from volume filter)
- Better quality from filtering thin bars
- Score in -0.13 to -0.14 range (slight improvement from #9's -0.1437)

The philosophy:
#9 works because the signal is real and simple. #27 failed because hard bounds
killed all trades. This approach keeps the signal simple but adds a soft filter:
prefer thicker bars (more reliable flow measurement) without hard cutoffs.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.55,
        stop_offset: float = 1.5,
        target_offset: float = 3.0,
        size: int = 1,
        min_volume: int = 100,
        max_entries_per_session: int = 3,
        volume_ratio_threshold: float = 0.9,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side (0.55 = 55%+)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Absolute minimum bar volume (hard floor)
            max_entries_per_session: Maximum entries per session
            volume_ratio_threshold: Require bar volume > session_avg * this ratio
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        self.volume_ratio_threshold = volume_ratio_threshold
        
        # Track entries per session
        self._current_session: int | None = None
        self._entries_this_session: int = 0
        self._bars_in_session: int = 0
        
        # Track session volume for relative filtering
        self._session_volumes: list[int] = []

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def _get_session_avg_volume(self) -> float:
        """
        Calculate average volume for current session.
        Returns a reasonable default if not enough data.
        """
        if not self._session_volumes:
            return float(self.min_volume)
        return sum(self._session_volumes) / len(self._session_volumes)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: #9 baseline with soft volume confirmation.
        Only enter when bar volume is above session average (soft filter).
        """
        if not ctx.actionable:
            return
        
        # Track session and bar count
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._entries_this_session = 0
            self._bars_in_session = 0
            self._session_volumes = []
        
        self._bars_in_session += 1
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        last_bar = history[-1]
        
        # Update session volume tracking
        self._session_volumes.append(last_bar.volume)
        
        # One position at a time
        if ctx.position or ctx.working_order_ids:
            return
        
        # Limit entries per session
        if self._entries_this_session >= self.max_entries_per_session:
            return
        
        if len(history) < 2:
            return
        
        # Skip first 3 bars of session (cash open microstructure)
        if self._bars_in_session <= 3:
            return
        
        # Skip last 5 minutes of session (forced liquidation)
        time_left = ctx.session_end - ctx.ts
        if time_left < 300_000_000_000:  # Less than 5 minutes (in nanoseconds)
            return
        
        # Hard floor: skip very thin bars
        if last_bar.volume < self.min_volume:
            return
        
        # Soft filter: prefer bars thicker than session average
        session_avg = self._get_session_avg_volume()
        if last_bar.volume < session_avg * self.volume_ratio_threshold:
            return
        
        # Measure aggression: ratio of dominant side to total volume
        buy_vol = last_bar.buy_volume
        sell_vol = last_bar.sell_volume
        total_vol = last_bar.volume
        
        if total_vol <= 0:
            return
        
        buy_ratio = buy_vol / total_vol
        sell_ratio = sell_vol / total_vol
        
        # Get previous bar for directional agreement
        prev_bar = history[-2]
        if prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        
        # Strong buy signal: current bar has 55%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Previous bar agreement: reject if strongly sell-dominated
            if prev_sell_ratio > self.flow_threshold:
                return
            
            # Price action confirmation: close above midpoint
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < 0.5:
                    return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # Strong sell signal: current bar has 55%+ sell volume
        elif sell_ratio > self.flow_threshold:
            # Previous bar agreement: reject if strongly buy-dominated
            if prev_buy_ratio > self.flow_threshold:
                return
            
            # Price action confirmation: close below midpoint
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position > 0.5:
                    return
            
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
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
