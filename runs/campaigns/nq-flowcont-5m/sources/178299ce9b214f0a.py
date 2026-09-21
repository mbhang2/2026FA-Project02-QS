"""
Flow continuation strategy: dual-threshold confirmation with asymmetric targets.

Analysis of best performers:
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → baseline
- #36: -0.1495, 768 trades → volume persistence helped slightly
- #33: -0.1740, 768 trades → good performer

Key insight from the trial history:
The best scores cluster around -0.14 to -0.15 with 768 trades (2.0/session).
This suggests the core flow signal at 0.55 threshold is near-optimal, and the
remaining edge comes from:
1. Filtering weak signals (volume, opposition checks work)
2. Better risk management (asymmetric targets based on signal strength)
3. Tighter entry selectivity without cutting trade count drastically

The problem with #36 and similar: we're treating all 0.55+ signals equally.
A bar with 0.55 buy ratio (barely above threshold) is different from one with
0.62 buy ratio (much stronger). The stronger signal deserves a more aggressive
target; the weaker one needs tighter risk.

New hypothesis - SIGNAL STRENGTH ASYMMETRY:
Instead of fixed stop/target offsets, scale them based on how strong the signal is:
1. Keep the core #36 logic: 0.55 threshold, opposition check, volume persistence
2. Measure signal strength: how far above 0.55 is the buy_ratio or sell_ratio?
   - Weak signal (0.55-0.58): stop=1.5, target=2.5 (tighter, more conservative)
   - Strong signal (0.58-0.62): stop=1.5, target=3.0 (standard)
   - Very strong signal (0.62+): stop=1.5, target=3.5 (let winners run)
3. This keeps trade count at 768 but improves quality by:
   - Protecting weak signals with tighter targets
   - Letting strong signals run further
   - Better risk/reward calibration per signal strength

Why this works:
- Commission is 0.20 per round turn; a weak signal needs tight profit to survive
- A strong signal (0.62+) has more follow-through potential
- This is not changing when we enter, just how much we let each position run
- Expected: 768 trades, better edge per trade
- Score improvement from better risk management

The philosophy:
#9 and #36 are good. The signal works. The issue is one-size-fits-all brackets
don't account for signal conviction. By scaling targets based on signal strength,
we improve the risk/reward of each trade without reducing trade count or adding
complexity. This is pure risk management refinement.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.55,
        opposition_threshold: float = 0.55,
        volume_ratio_min: float = 0.80,
        stop_offset: float = 1.5,
        target_offset_weak: float = 2.5,
        target_offset_normal: float = 3.0,
        target_offset_strong: float = 3.5,
        strong_signal_threshold: float = 0.62,
        normal_signal_threshold: float = 0.58,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio for entry signal (0.55 = 55%+)
            opposition_threshold: Reject entry if prev bar shows opposite at this
                                 level (0.55 = reject if 55%+ opposite)
            volume_ratio_min: Current bar volume must be >= this ratio of prev bar
                             (0.80 = current >= 80% of previous volume)
            stop_offset: Distance for protective stop (1.5 points, fixed)
            target_offset_weak: Target for weak signals 0.55-0.58 (2.5 points)
            target_offset_normal: Target for normal signals 0.58-0.62 (3.0 points)
            target_offset_strong: Target for strong signals 0.62+ (3.5 points)
            strong_signal_threshold: Ratio threshold for strong signal (0.62)
            normal_signal_threshold: Ratio threshold for normal signal (0.58)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.opposition_threshold = opposition_threshold
        self.volume_ratio_min = volume_ratio_min
        self.stop_offset = stop_offset
        self.target_offset_weak = target_offset_weak
        self.target_offset_normal = target_offset_normal
        self.target_offset_strong = target_offset_strong
        self.strong_signal_threshold = strong_signal_threshold
        self.normal_signal_threshold = normal_signal_threshold
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        
        # Track entries per session
        self._current_session: int | None = None
        self._entries_this_session: int = 0
        self._bars_in_session: int = 0

    def _get_target_offset(self, signal_strength: float) -> float:
        """
        Determine target offset based on signal strength.
        
        signal_strength: the buy_ratio or sell_ratio (0.55 to 1.0)
        Returns: appropriate target_offset for this signal strength
        """
        if signal_strength >= self.strong_signal_threshold:
            return self.target_offset_strong
        elif signal_strength >= self.normal_signal_threshold:
            return self.target_offset_normal
        else:
            return self.target_offset_weak

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: 5m flow signal with volume persistence check and
        asymmetric targets based on signal strength.
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
        
        # Get previous bar for opposition and volume checks
        prev_bar = history[-2]
        
        if prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        
        # Volume persistence check: current bar should have reasonable volume
        # relative to previous bar (not a thin bar that might reverse)
        volume_ratio = last_bar.volume / prev_bar.volume
        if volume_ratio < self.volume_ratio_min:
            return
        
        # Strong buy signal: current bar has 55%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Opposition check: reject if previous bar was strongly sell-dominated
            if prev_sell_ratio > self.opposition_threshold:
                return
            
            # Price action confirmation: close above midpoint
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < 0.5:
                    return
            
            # Determine target based on signal strength
            target = self._get_target_offset(buy_ratio)
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=target,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # Strong sell signal: current bar has 55%+ sell volume
        elif sell_ratio > self.flow_threshold:
            # Opposition check: reject if previous bar was strongly buy-dominated
            if prev_buy_ratio > self.opposition_threshold:
                return
            
            # Price action confirmation: close below midpoint
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position > 0.5:
                    return
            
            # Determine target based on signal strength
            target = self._get_target_offset(sell_ratio)
            
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=target,
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
