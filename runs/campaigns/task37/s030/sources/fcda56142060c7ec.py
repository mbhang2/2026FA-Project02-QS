"""
Analysis of trials #0-#89 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers cluster around +1.54 with 680-700 trades
2. All single-signal refinements hit diminishing returns around +1.54
3. The core MA crossover (6/20) with volatility-scaled brackets is proven robust
4. Trials #87-#89 added momentum, trend strength, and price action but plateaued
5. What we haven't explored: MULTI-SIGNAL ENSEMBLE WITH DYNAMIC WEIGHTING

Critical insight - SIGNAL ENSEMBLE WITH ADAPTIVE WEIGHTING:
The plateau at +1.54 suggests no single confirmation signal is sufficient. Current approaches
use one primary signal + one secondary confirmation, but what if we need THREE signals
working together, weighted by their recent predictive power?

New hypothesis - ADAPTIVE TRIPLE-SIGNAL ENSEMBLE:
Instead of requiring all three signals to align perfectly, use a WEIGHTED SCORING system:

1. Keep the MA crossover framework (fast > slow for buys, fast < slow for sells) as base
2. Three confirmation signals, each scored 0-1:
   - SIGNAL A: Order flow confirmation (delta/volume ratio strength)
   - SIGNAL B: Price action confirmation (rejection of opposite extreme)
   - SIGNAL C: Momentum confirmation (bar closed > midpoint of last 5 bars in trend direction)
3. ADAPTIVE WEIGHTING based on recent signal accuracy:
   - Track which signals correctly predicted the last N trades
   - Weight recent winners higher, recent losers lower
   - Rebalance weights every 20 bars to adapt to market regime
4. Entry threshold: weighted_score > 1.8 (out of 3.0)
   - In choppy markets: signals will be uncorrelated, threshold naturally rises
   - In trending markets: signals align, threshold naturally satisfied
5. Tighter parameters:
   - vol_multiplier_stop: 1.05 (aggressive stops)
   - vol_multiplier_target: 1.56 (wider targets)
   - min_ma_divergence: 0.00130 (slightly lower to catch more trends)

The insight: #89's price action filter was good but too restrictive. What if instead of
requiring all signals, we use a WEIGHTED ENSEMBLE where recent winners get higher weight?
This adapts to market regime without manual parameter tuning, and captures more valid
entries while filtering false ones through the ensemble.

Expected outcome: Exceed #63's +1.5421 by using adaptive signal weighting that captures
the best of each confirmation type while filtering through ensemble consensus.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 price_action_lookback: int = 5, momentum_lookback: int = 5,
                 vol_multiplier_stop: float = 1.05,
                 vol_multiplier_target: float = 1.56,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00130,
                 order_flow_threshold: float = 0.10,
                 price_tolerance: float = 0.0021,
                 ensemble_threshold: float = 1.8,
                 weight_rebalance_bars: int = 20,
                 size: int = 1):
        self.interval = interval
        self.fast_lookback = fast_lookback
        self.slow_lookback = slow_lookback
        self.vol_lookback = vol_lookback
        self.price_action_lookback = price_action_lookback
        self.momentum_lookback = momentum_lookback
        self.vol_multiplier_stop = vol_multiplier_stop
        self.vol_multiplier_target = vol_multiplier_target
        self.min_stop = min_stop
        self.max_stop = max_stop
        self.min_target = min_target
        self.max_target = max_target
        self.min_ma_divergence = min_ma_divergence
        self.order_flow_threshold = order_flow_threshold
        self.price_tolerance = price_tolerance
        self.ensemble_threshold = ensemble_threshold
        self.weight_rebalance_bars = weight_rebalance_bars
        self.size = size
        
        # Accumulate closes, highs, lows
        self.closes: list[float] = []
        self.highs: list[float] = []
        self.lows: list[float] = []
        
        # Accumulate true ranges for ATR-like volatility
        self.true_ranges: list[float] = []
        self.prev_close: float | None = None
        
        # Track volatility history
        self.vol_history: list[float] = []
        
        # Signal weights (adaptive)
        self.weight_order_flow: float = 1.0
        self.weight_price_action: float = 1.0
        self.weight_momentum: float = 1.0
        
        # Track recent trades for weight adaptation
        self.recent_trades: list[dict] = []  # {side, order_flow, price_action, momentum, filled}
        self.bars_since_rebalance: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _update_signal_weights(self) -> None:
        """Rebalance signal weights based on recent trade accuracy."""
        if len(self.recent_trades) < 5:
            return
        
        # Look at last 10 trades
        recent = self.recent_trades[-10:]
        
        order_flow_correct = sum(1 for t in recent if t.get("filled") and t.get("order_flow_signal"))
        price_action_correct = sum(1 for t in recent if t.get("filled") and t.get("price_action_signal"))
        momentum_correct = sum(1 for t in recent if t.get("filled") and t.get("momentum_signal"))
        
        total = len([t for t in recent if t.get("filled")])
        if total == 0:
            return
        
        # Adjust weights based on accuracy (with smoothing to avoid wild swings)
        alpha = 0.3
        
        if order_flow_correct > 0:
            self.weight_order_flow = (1 - alpha) * self.weight_order_flow + alpha * (1.0 + order_flow_correct / total * 0.5)
        else:
            self.weight_order_flow = (1 - alpha) * self.weight_order_flow + alpha * 0.7
        
        if price_action_correct > 0:
            self.weight_price_action = (1 - alpha) * self.weight_price_action + alpha * (1.0 + price_action_correct / total * 0.5)
        else:
            self.weight_price_action = (1 - alpha) * self.weight_price_action + alpha * 0.7
        
        if momentum_correct > 0:
            self.weight_momentum = (1 - alpha) * self.weight_momentum + alpha * (1.0 + momentum_correct / total * 0.5)
        else:
            self.weight_momentum = (1 - alpha) * self.weight_momentum + alpha * 0.7
        
        # Normalize weights
        total_weight = self.weight_order_flow + self.weight_price_action + self.weight_momentum
        if total_weight > 0:
            self.weight_order_flow /= total_weight
            self.weight_price_action /= total_weight
            self.weight_momentum /= total_weight

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last_bar = history[-1]
        current_price = last_bar.close

        # Accumulate closes, highs, lows
        self.closes.append(current_price)
        self.highs.append(last_bar.high)
        self.lows.append(last_bar.low)
        
        # Calculate true range (ATR component)
        high_low = last_bar.high - last_bar.low
        if self.prev_close is not None:
            high_prev = abs(last_bar.high - self.prev_close)
            low_prev = abs(last_bar.low - self.prev_close)
            true_range = max(high_low, high_prev, low_prev)
        else:
            true_range = high_low
        
        self.true_ranges.append(true_range)
        self.prev_close = current_price
        
        # Keep only what we need
        max_needed = self.slow_lookback + self.vol_lookback + self.price_action_lookback + self.momentum_lookback + 10
        if len(self.closes) > max_needed:
            self.closes = self.closes[-max_needed:]
        if len(self.highs) > max_needed:
            self.highs = self.highs[-max_needed:]
        if len(self.lows) > max_needed:
            self.lows = self.lows[-max_needed:]
        if len(self.true_ranges) > max_needed:
            self.true_ranges = self.true_ranges[-max_needed:]

        # Need enough data for both moving averages
        if len(self.closes) < self.slow_lookback:
            return

        # Calculate fast and slow moving averages
        fast_closes = self.closes[-self.fast_lookback:]
        slow_closes = self.closes[-self.slow_lookback:]
        
        fast_ma = sum(fast_closes) / len(fast_closes)
        slow_ma = sum(slow_closes) / len(slow_closes)
        
        if slow_ma <= 0:
            return
        
        # Calculate volatility using ATR (average true range)
        vol = 0.0
        if len(self.true_ranges) >= self.vol_lookback:
            recent_ranges = self.true_ranges[-self.vol_lookback:]
            atr = sum(recent_ranges) / len(recent_ranges)
            if current_price > 0:
                vol = atr / current_price
        
        self.vol_history.append(vol)
        if len(self.vol_history) > self.vol_lookback * 2:
            self.vol_history = self.vol_history[-(self.vol_lookback * 2):]
        
        # SIGNAL A: Order flow confirmation
        total_volume = last_bar.volume
        order_flow_signal = 0.0
        if total_volume > 0:
            delta = last_bar.delta
            order_flow_ratio = delta / total_volume
            if order_flow_ratio > self.order_flow_threshold:
                order_flow_signal = min(1.0, abs(order_flow_ratio) / 0.3)
            elif order_flow_ratio < -self.order_flow_threshold:
                order_flow_signal = min(1.0, abs(order_flow_ratio) / 0.3)
        
        # SIGNAL B: Price action confirmation
        price_action_signal_buy = 0.0
        price_action_signal_sell = 0.0
        
        if len(self.highs) >= self.price_action_lookback and len(self.lows) >= self.price_action_lookback:
            recent_highs = self.highs[-self.price_action_lookback:]
            recent_lows = self.lows[-self.price_action_lookback:]
            
            range_high = max(recent_highs)
            range_low = min(recent_lows)
            range_mid = (range_high + range_low) / 2.0
            
            if range_high > range_low:
                # Buy confirmation: how far above midpoint
                if current_price > range_mid:
                    price_action_signal_buy = (current_price - range_mid) / (range_high - range_mid)
                
                # Sell confirmation: how far below midpoint
                if current_price < range_mid:
                    price_action_signal_sell = (range_mid - current_price) / (range_mid - range_low)
        
        # SIGNAL C: Momentum confirmation
        momentum_signal_buy = 0.0
        momentum_signal_sell = 0.0
        
        if len(self.closes) >= self.momentum_lookback:
            recent_closes = self.closes[-self.momentum_lookback:]
            momentum_closes_above_mid = sum(1 for c in recent_closes if c > (min(recent_closes) + max(recent_closes)) / 2.0)
            momentum_closes_below_mid = self.momentum_lookback - momentum_closes_above_mid
            
            # Buy: more closes above midpoint
            momentum_signal_buy = momentum_closes_above_mid / self.momentum_lookback
            # Sell: more closes below midpoint
            momentum_signal_sell = momentum_closes_below_mid / self.momentum_lookback
        
        # Calculate MA divergence (normalized)
        ma_divergence = abs(fast_ma - slow_ma) / slow_ma
        
        # Only trade if MAs are sufficiently diverged
        if ma_divergence < self.min_ma_divergence:
            return
        
        # Rebalance weights periodically
        self.bars_since_rebalance += 1
        if self.bars_since_rebalance >= self.weight_rebalance_bars:
            self._update_signal_weights()
            self.bars_since_rebalance = 0
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band
        tolerance_band = self.price_tolerance * slow_ma
        
        # Buy: uptrend + weighted signal ensemble
        if fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and last_bar.close > last_bar.open:
            buy_ensemble_score = (
                self.weight_order_flow * (order_flow_signal if order_flow_signal > 0 else 0) +
                self.weight_price_action * price_action_signal_buy +
                self.weight_momentum * momentum_signal_buy
            )
            
            if buy_ensemble_score > self.ensemble_threshold:
                trade_record = {
                    "side": "buy",
                    "order_flow_signal": order_flow_signal > 0,
                    "price_action_signal": price_action_signal_buy > 0.5,
                    "momentum_signal": momentum_signal_buy > 0.6,
                    "filled": False,
                }
                self.recent_trades.append(trade_record)
                if len(self.recent_trades) > 50:
                    self.recent_trades = self.recent_trades[-50:]
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry",
                )
        
        # Sell: downtrend + weighted signal ensemble
        elif fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and last_bar.close < last_bar.open:
            sell_ensemble_score = (
                self.weight_order_flow * (order_flow_signal if order_flow_signal > 0 else 0) +
                self.weight_price_action * price_action_signal_sell +
                self.weight_momentum * momentum_signal_sell
            )
            
            if sell_ensemble_score > self.ensemble_threshold:
                trade_record = {
                    "side": "sell",
                    "order_flow_signal": order_flow_signal > 0,
                    "price_action_signal": price_action_signal_sell > 0.5,
                    "momentum_signal": momentum_signal_sell > 0.6,
                    "filled": False,
                }
                self.recent_trades.append(trade_record)
                if len(self.recent_trades) > 50:
                    self.recent_trades = self.recent_trades[-50:]
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry",
                )

    def on_fill(self, ctx) -> None:
        if self.recent_trades:
            self.recent_trades[-1]["filled"] = True

    def on_lifecycle(self, ctx) -> None:
        pass
