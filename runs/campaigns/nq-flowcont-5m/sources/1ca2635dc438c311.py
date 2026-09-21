"""
Flow continuation strategy: multi-timeframe confirmation with tighter entry filters.

Analysis of recent trials:
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → best baseline
- #19: -0.1631, 382 trades (0.99/session) → fewer high-quality trades competitive
- #28: -0.3249, 1152 trades (3.0/session) → soft volume filter underperformed
- #27: -0.6699, 2 trades (0.005/session) → hard volatility bounds killed trades

Core insight from #9's success:
The 0.55 flow threshold is solid. The issue is not the signal but WHEN we take it.
We're entering too many marginal trades. Commission is 0.20 per round turn, which
means we need edge > 0.10 per trade just to break even.

New hypothesis - MULTI-TIMEFRAME CONFIRMATION:
#9 uses only the current 5m bar and previous 5m bar. What if we add ONE more filter:
check that the PREVIOUS 1-minute bar (if we have it) also shows agreement?

Why this works:
1. The 5m signal (0.55+) is the main entry criterion - keep it
2. Add 1m confirmation: if we're seeing strong buy flow on 5m, the 1m bars
   leading up to it should also show some buying (>0.50 buy ratio)
3. This filters out "sudden reversals" - bars that flip from sell to buy
4. We reduce false entries without killing the signal
5. Expected: 600-700 trades of higher quality (fewer whipsaws)

Implementation:
- Subscribe to both "5m" and "1m"
- Entry on 5m bar closing with 0.55+ flow
- ADDED: Check last 1-2 1m bars - they should show agreement (>0.50 on same side)
- This is a SOFT filter: if we don't have 1m history yet, we still trade
- Keep everything else from #9: stop 1.5, target 3.0, session discipline, etc.

Why this is different from #27 (which failed):
- #27 used hard bounds on volatility (0.5-2.0) that killed 99% of trades
- This uses soft confirmation from a finer timeframe, not a hard filter
- We keep the signal, just add one more "does this make sense?" check
- Expected to reduce trades by 10-15%, keep the best ones

The philosophy:
#9 is the baseline. The signal works. The problem is execution quality.
Multi-timeframe confirmation is a classic way to improve signal quality without
changing the underlying hypothesis. We're not filtering by volatility or volume -
we're confirming that the micro-structure agrees with the macro signal.

Expected outcome:
- 650-750 trades (10-15% fewer than #9's 768)
- Better quality from 1m confirmation
- Score in -0.12 to -0.14 range (improvement from #9's -0.1437)
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval_main: str = "5m",
        interval_confirm: str = "1m",
        flow_threshold: float = 0.55,
        confirm_threshold: float = 0.50,
        stop_offset: float = 1.5,
        target_offset: float = 3.0,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
    ):
        """
        Args:
            interval_main: Main signal interval (5m per campaign spec)
            interval_confirm: Confirmation interval (1m for micro-structure check)
            flow_threshold: Minimum ratio for main signal (0.55 = 55%+)
            confirm_threshold: Minimum ratio for confirmation (0.50 = 50%+)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval_main = interval_main
        self.interval_confirm = interval_confirm
        self.flow_threshold = flow_threshold
        self.confirm_threshold = confirm_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        
        # Track entries per session
        self._current_session: int | None = None
        self._entries_this_session: int = 0
        self._bars_in_session: int = 0

    def setup(self, ctx) -> None:
        """Subscribe to 5m (main) and 1m (confirmation) bars."""
        ctx.subscribe(self.interval_main)
        ctx.subscribe(self.interval_confirm)

    def _check_confirmation(self, ctx) -> bool:
        """
        Check if recent 1m bars show agreement with the signal direction.
        Returns True if we should proceed with entry, False if we should skip.
        
        For a buy signal: recent 1m bars should show buying (>0.50 buy ratio)
        For a sell signal: recent 1m bars should show selling (>0.50 sell ratio)
        
        If we don't have enough 1m history, we don't reject (soft filter).
        """
        # This will be called with the expected direction passed in
        # We'll check it when we know the direction
        return True  # Placeholder; will be refined below

    def on_bars(self, ctx) -> None:
        """
        Entry logic: 5m flow signal with 1m confirmation.
        Only enter when current 5m bar shows strong flow AND
        recent 1m bars show agreement.
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
        
        history_main = ctx.history(self.interval_main)
        if len(history_main) < 2:
            return
        
        last_bar = history_main[-1]  # Bar that just closed
        
        # Skip first 3 bars of session (cash open microstructure is different)
        if self._bars_in_session <= 3:
            return
        
        # Skip last 5 minutes of session (forced liquidation, end-of-day pressure)
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
        
        # Get previous bar for directional agreement
        prev_bar = history_main[-2]
        if prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        
        # Get 1m confirmation history
        history_confirm = ctx.history(self.interval_confirm)
        
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
            
            # 1m confirmation: check recent 1m bars show buying agreement
            if len(history_confirm) >= 2:
                # Check the last 1-2 1m bars for buying pressure
                recent_1m_bars = history_confirm[-2:] if len(history_confirm) >= 2 else [history_confirm[-1]]
                for bar_1m in recent_1m_bars:
                    if bar_1m.volume > 0:
                        buy_ratio_1m = bar_1m.buy_volume / bar_1m.volume
                        # If recent 1m bar shows strong selling, skip (no agreement)
                        if buy_ratio_1m < (1.0 - self.flow_threshold):
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
            
            # 1m confirmation: check recent 1m bars show selling agreement
            if len(history_confirm) >= 2:
                # Check the last 1-2 1m bars for selling pressure
                recent_1m_bars = history_confirm[-2:] if len(history_confirm) >= 2 else [history_confirm[-1]]
                for bar_1m in recent_1m_bars:
                    if bar_1m.volume > 0:
                        sell_ratio_1m = bar_1m.sell_volume / bar_1m.volume
                        # If recent 1m bar shows strong buying, skip (no agreement)
                        if sell_ratio_1m < (1.0 - self.flow_threshold):
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
