"""
Flow continuation strategy: adaptive signal strength with session-aware timing.

Analysis of the trial history reveals:
- Best performers: #9, #10, #15 at -0.1437 with 768 trades (2.0/session)
- #38 (temporal clustering): -0.3019, only 2 trades → confirmation was too strict
- #37 (asymmetric targets): -0.1865 → over-complicated hurt performance
- Consistent pattern: simple, consistent 768 trades with 0.55 threshold works best

The core insight from 38 trials:
1. The -0.1437 score is remarkably stable across many variations
2. Trade count of 768 (2.0/session) is the sweet spot
3. Commission is 0.20 per round turn; we need consistent edge
4. Complexity consistently hurts; simplicity wins

The remaining problem:
We're at break-even after commission. Each trade makes ~0.20 before commission,
loses 0.20 to commission, net ~-0.14. To improve, we need:
1. Better entry timing (not just signal strength)
2. Better risk management (not asymmetric targets, but adaptive exits)
3. Session-aware adjustments (some sessions are noisier than others)

New hypothesis - ADAPTIVE SIGNAL WITH SESSION VOLATILITY:
Instead of fixed thresholds everywhere, adapt to session characteristics:
1. Keep the core 0.55 flow threshold (it works)
2. Track session volatility: measure range of bars 4-10 in each session
3. On high-volatility sessions: require 0.57+ threshold (filter noise)
4. On low-volatility sessions: allow 0.54+ threshold (catch smaller moves)
5. Keep stop at 1.5, target at 3.0 (proven offsets)
6. Add one more filter: only enter if the signal bar's range is > session median
   (avoid thin bars that are noise)

Why this works:
- High-volatility sessions have more false signals; stricter threshold filters them
- Low-volatility sessions have real signals; looser threshold catches them
- Signal bar range check ensures we're not trading the noise bars
- Trade count stays ~768 (maybe 750-800 range)
- Edge improves from session-aware filtering

Why we haven't tried this:
- Requires tracking session metrics (volatility, median range)
- But it's the natural next step after proving the signal works

Implementation:
1. In setup(), scan train_bars to understand typical session volatility
2. Track session volatility in on_bars (rolling measure of recent bar ranges)
3. Adapt flow_threshold based on current session volatility
4. Keep everything else from #9 (opposition check, volume filter, timing)
5. Add bar range filter: only enter if bar.high - bar.low > session_median_range

Expected outcome:
- 750-800 trades (slight reduction from 768)
- Better signal quality from volatility-aware filtering
- Score improvement to -0.12 to -0.13 range
- More robust across different session types
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold_base: float = 0.55,
        flow_threshold_high_vol: float = 0.57,
        flow_threshold_low_vol: float = 0.54,
        opposition_threshold: float = 0.55,
        stop_offset: float = 1.5,
        target_offset: float = 3.0,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
        volatility_window: int = 20,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold_base: Base threshold for flow signal (0.55)
            flow_threshold_high_vol: Threshold for high-volatility sessions (0.57)
            flow_threshold_low_vol: Threshold for low-volatility sessions (0.54)
            opposition_threshold: Reject if prev bar shows opposite at this level
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
            volatility_window: Number of bars to measure session volatility
        """
        self.interval = interval
        self.flow_threshold_base = flow_threshold_base
        self.flow_threshold_high_vol = flow_threshold_high_vol
        self.flow_threshold_low_vol = flow_threshold_low_vol
        self.opposition_threshold = opposition_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        self.volatility_window = volatility_window
        
        # Track entries per session
        self._current_session: int | None = None
        self._entries_this_session: int = 0
        self._bars_in_session: int = 0
        
        # Track bar ranges for volatility measurement
        self._session_bar_ranges: list[float] = []
        self._median_bar_range: float = 0.0

    def _calculate_median(self, values: list[float]) -> float:
        """Calculate median of a list."""
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        if n % 2 == 1:
            return sorted_vals[n // 2]
        else:
            return (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2.0

    def _get_session_volatility_level(self) -> str:
        """
        Determine volatility level based on median bar range.
        Returns 'high', 'low', or 'normal'.
        """
        if not self._session_bar_ranges or len(self._session_bar_ranges) < 5:
            return "normal"
        
        median = self._calculate_median(self._session_bar_ranges)
        
        # Classify based on deviation from base
        recent_avg = sum(self._session_bar_ranges[-5:]) / 5.0
        
        if recent_avg > median * 1.2:
            return "high"
        elif recent_avg < median * 0.8:
            return "low"
        else:
            return "normal"

    def _get_flow_threshold(self) -> float:
        """Get adaptive flow threshold based on session volatility."""
        vol_level = self._get_session_volatility_level()
        
        if vol_level == "high":
            return self.flow_threshold_high_vol
        elif vol_level == "low":
            return self.flow_threshold_low_vol
        else:
            return self.flow_threshold_base

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: adaptive flow signal with session-aware thresholds.
        Volatility is measured from bar ranges; thresholds adjust accordingly.
        """
        if not ctx.actionable:
            return
        
        # Track session and bar count
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._entries_this_session = 0
            self._bars_in_session = 0
            self._session_bar_ranges = []
            self._median_bar_range = 0.0
        
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
        
        # Track bar range for volatility measurement
        bar_range = last_bar.high - last_bar.low
        self._session_bar_ranges.append(bar_range)
        
        # Keep only recent bars for volatility calculation
        if len(self._session_bar_ranges) > self.volatility_window:
            self._session_bar_ranges.pop(0)
        
        # Update median bar range
        if len(self._session_bar_ranges) >= 5:
            self._median_bar_range = self._calculate_median(self._session_bar_ranges)
        
        # Skip first 3 bars of session
        if self._bars_in_session <= 3:
            return
        
        # Skip last 5 minutes of session
        time_left = ctx.session_end - ctx.ts
        if time_left < 300_000_000_000:
            return
        
        # Skip low-volume bars
        if last_bar.volume < self.min_volume:
            return
        
        # Skip thin bars (noise): bar range must be above session median
        if self._median_bar_range > 0 and bar_range < self._median_bar_range * 0.7:
            return
        
        # Measure flow ratios
        buy_vol = last_bar.buy_volume
        sell_vol = last_bar.sell_volume
        total_vol = last_bar.volume
        
        if total_vol <= 0:
            return
        
        buy_ratio = buy_vol / total_vol
        sell_ratio = sell_vol / total_vol
        
        # Get previous bar for opposition check
        prev_bar = history[-2]
        
        if prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        
        # Get adaptive flow threshold based on current session volatility
        flow_threshold = self._get_flow_threshold()
        
        # Strong buy signal
        if buy_ratio > flow_threshold:
            # Opposition check
            if prev_sell_ratio > self.opposition_threshold:
                return
            
            # Price action confirmation: close above midpoint
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
        
        # Strong sell signal
        elif sell_ratio > flow_threshold:
            # Opposition check
            if prev_buy_ratio > self.opposition_threshold:
                return
            
            # Price action confirmation: close below midpoint
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
        """Position filled. Bracket handles the exit."""
        pass

    def on_lifecycle(self, ctx) -> None:
        """Track lifecycle events."""
        pass
