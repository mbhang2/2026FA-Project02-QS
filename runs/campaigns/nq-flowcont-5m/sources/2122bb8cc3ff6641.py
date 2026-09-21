"""
Flow continuation strategy: aggressive signal strength with dynamic risk-reward.

Analysis of recent trials:
- #25: -0.2991, 767 trades (2.0/session) → dual-threshold approach hurt
- #24: -0.2466, 768 trades (2.0/session) → 1m confirmation added noise
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → best baseline
- #19: -0.1631, 382 trades (0.99/session) → fewer, higher-quality trades worked

Key insight from the data:
The best score (-0.1437) came from #9's simple approach with 768 trades at 0.55 threshold.
Every attempt to improve has failed by adding complexity.

BUT: Looking at #19's -0.1631 with only 382 trades (0.99/session), we see that
FEWER, HIGHER-QUALITY trades can be competitive. The commission cost per trade
is fixed (~0.20 index points per round turn), so the only way to improve is:
1. Higher edge per trade (better signal quality)
2. Better risk management (dynamic risk-reward based on signal strength)

New approach - SIGNAL STRENGTH SIZING WITH DYNAMIC RISK-REWARD:
Instead of treating all 0.55+ signals equally, use signal strength to:
1. Determine WHETHER to trade (only highest conviction)
2. Determine HOW MUCH to risk (dynamic stop based on signal strength)
3. Determine TARGET (dynamic target based on signal strength)

The hypothesis:
- Weak signals (0.55-0.58): Skip entirely (too noisy, commission not worth it)
- Medium signals (0.58-0.62): Trade with tight risk (stop 1.0, target 2.5)
- Strong signals (0.62+): Trade with wider risk (stop 2.0, target 4.0)

This means:
- We trade fewer times (fewer weak signals) → lower commission drag
- We risk more on strong signals → better reward on high-conviction trades
- We risk less on medium signals → cap losses on borderline trades
- Expected: 500-700 trades of much better quality

Why this works:
1. Commission is killing us at -0.1437 with 768 trades = 2 trades/session
2. At 0.20 per round turn, that's ~0.10 commission per session baseline
3. We need to either: (a) make bigger wins, or (b) trade less on weak signals
4. Signal strength tells us which is which
5. A 0.65+ buy bar has MUCH better odds than a 0.55 buy bar
6. We should risk more on the 0.65 and skip the 0.55

The logic:
- First 3 bars skip (microstructure)
- Last 5 minutes skip (forced liquidation)
- Volume >150 (reliability)
- Previous bar check: reject if strongly opposed
- Price action: close in correct half
- THEN: based on signal strength ratio:
  - 0.55-0.58: skip (too weak, commission not worth it)
  - 0.58-0.62: size=1, stop=1.0, target=2.5 (medium conviction)
  - 0.62+: size=1, stop=2.0, target=4.0 (high conviction)
- Max 3 entries per session (we'll get fewer due to weak signal filtering)

Expected outcome:
- 500-700 trades (fewer than #9's 768)
- Better quality from filtering weak signals
- Better risk-reward on remaining trades
- Score in -0.10 to -0.13 range

The philosophy:
#9 worked because the signal is real. We're not going to beat it by adding
noise. We're going to beat it by: (1) trading only the highest-conviction
signals, and (2) sizing our risk appropriately to signal strength.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        weak_threshold: float = 0.58,
        medium_threshold: float = 0.62,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 3,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            weak_threshold: Below this, skip the signal (0.58 = 58%+)
            medium_threshold: Above this, use strong parameters (0.62 = 62%+)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.weak_threshold = weak_threshold
        self.medium_threshold = medium_threshold
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        
        # Track entries per session
        self._current_session: int | None = None
        self._entries_this_session: int = 0
        self._bars_in_session: int = 0

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: filter by signal strength and size risk accordingly.
        Weak signals (0.55-0.58): skip entirely.
        Medium signals (0.58-0.62): tight risk (stop 1.0, target 2.5).
        Strong signals (0.62+): wider risk (stop 2.0, target 4.0).
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
        prev_bar = history[-2]
        if prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        
        # Price action confirmation helper
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            return
        
        close_position = (last_bar.close - last_bar.low) / bar_range
        
        # BUY SIGNAL: current bar has strong buy volume
        if buy_ratio > self.weak_threshold:
            # Previous bar check: reject if strongly sell-dominated
            if prev_sell_ratio > 0.55:
                return
            
            # Price action confirmation: close above midpoint
            if close_position < 0.5:
                return
            
            # Determine risk-reward based on signal strength
            if buy_ratio >= self.medium_threshold:
                # STRONG signal: wider risk
                stop_offset = 2.0
                target_offset = 4.0
            else:
                # MEDIUM signal: tight risk
                stop_offset = 1.0
                target_offset = 2.5
            
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # SELL SIGNAL: current bar has strong sell volume
        elif sell_ratio > self.weak_threshold:
            # Previous bar check: reject if strongly buy-dominated
            if prev_buy_ratio > 0.55:
                return
            
            # Price action confirmation: close below midpoint
            if close_position > 0.5:
                return
            
            # Determine risk-reward based on signal strength
            if sell_ratio >= self.medium_threshold:
                # STRONG signal: wider risk
                stop_offset = 2.0
                target_offset = 4.0
            else:
                # MEDIUM signal: tight risk
                stop_offset = 1.0
                target_offset = 2.5
            
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
