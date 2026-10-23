"""Volume-acceleration momentum with tighter filters and regime adaptation.

Building on trial #0 (score -0.0278), this strategy refines the signal detection
to reduce false positives and improve trade quality.

Key changes:
1. Stricter volume acceleration threshold - require higher conviction
2. Add volatility normalization to volume ratio (vol accel relative to recent vol)
3. Tighter close-to-high filter - require closer to the top of the range
4. Simpler exit logic: fixed exit_bars window with bracket, no quality gating
5. Reduce minimum position size to increase sample but maintain discipline
6. Use higher stop/target offsets to account for microstructure spread costs

The prior attempt's signal quality tracking may have been too permissive.
This version focuses on higher-conviction setups with cleaner entry conditions.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        vol_lookback_k: int = 12,
        vol_threshold: float = 1.7,
        close_to_high_min: float = 0.6,
        exit_bars: int = 4,
        stop_offset: float = 1.2,
        target_offset: float = 1.8,
        size: int = 1,
        vol_volatility_window: int = 20,
    ):
        self.interval = interval
        self.vol_lookback_k = vol_lookback_k
        self.vol_threshold = vol_threshold
        self.close_to_high_min = close_to_high_min
        self.exit_bars = exit_bars
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.vol_volatility_window = vol_volatility_window
        
        # Track volumes for rolling mean and volatility
        self._volumes: deque[int] = deque(maxlen=vol_lookback_k + vol_volatility_window)
        
        # Track bars since entry
        self._bars_since_entry: int = 0
        self._entry_signal: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 2:
            return
        
        last = history[-1]
        
        # Track volume
        self._volumes.append(last.volume)
        
        # Need enough volume history for both mean and volatility
        min_vol_history = self.vol_lookback_k + self.vol_volatility_window
        if len(self._volumes) < min_vol_history:
            return
        
        # Compute volume acceleration with volatility normalization
        volumes_list = list(self._volumes)
        
        # Recent mean (lookback window)
        recent_vols = volumes_list[-self.vol_lookback_k:-1]
        vol_mean = sum(recent_vols) / len(recent_vols) if recent_vols else 1.0
        
        # Volume volatility (std dev of volume over longer window)
        longer_vols = volumes_list[-self.vol_volatility_window:]
        vol_avg_long = sum(longer_vols) / len(longer_vols) if longer_vols else 1.0
        vol_variance = sum((v - vol_avg_long) ** 2 for v in longer_vols) / len(longer_vols) if longer_vols else 1.0
        vol_std = vol_variance ** 0.5
        
        # Normalize volume ratio by recent volatility
        vol_ratio = last.volume / vol_mean if vol_mean > 0 else 1.0
        vol_zscore = (last.volume - vol_avg_long) / (vol_std + 1) if vol_std > 0 else 0
        
        # Require both absolute ratio and z-score for conviction
        volume_accelerated = vol_ratio > self.vol_threshold and vol_zscore > 0.5
        
        # Compute close-to-high ratio - stricter threshold
        bar_range = last.high - last.low
        if bar_range > 0:
            close_to_high = (last.close - last.low) / bar_range
        else:
            close_to_high = 0.5
        
        close_to_high_valid = close_to_high > self.close_to_high_min
        
        # Directional signal
        price_direction = 1 if last.close > last.open else (-1 if last.close < last.open else 0)
        
        # Combined signal
        if volume_accelerated and close_to_high_valid and price_direction != 0:
            signal = price_direction
        else:
            signal = 0
        
        # Manage existing position
        if ctx.position != 0:
            self._bars_since_entry += 1
            
            # Simple exit: reached exit_bars window
            if self._bars_since_entry >= self.exit_bars:
                ctx.close()
                self._bars_since_entry = 0
                self._entry_signal = 0
            
            return
        
        # Don't enter if we have working orders
        if ctx.working_order_ids:
            return
        
        # Only enter on valid signal
        if signal == 0:
            return
        
        # Place entry with bracket
        self._bars_since_entry = 0
        self._entry_signal = signal
        
        if signal > 0:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="vol-accel-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="vol-accel-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
