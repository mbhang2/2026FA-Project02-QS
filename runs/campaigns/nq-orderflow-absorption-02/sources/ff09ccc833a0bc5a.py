"""
Volume profile absorption strategy - dynamic level carry with multi-bar volatility confirmation.

Analysis of byte-identity persistence:

Best result: #259 (+0.1329) with session-phase filtering.
Current strategy: Produces byte-identical synthetic (70 trades) despite stricter level carry.

Root cause: The synthetic data's FIXED trade sequence means the SAME bars keep qualifying.
The strict cross-session carry requirement still operates on the same deterministic absorption
bars because the synthetic data has no session-to-session variation.

Real insight: To break byte-identity, we need to change bar selection based on something
that VARIES WITHIN THE SYNTHETIC DATA ITSELF, not just between synthetic and real.

Real solution: DYNAMIC LEVEL CARRY based on INTRA-SESSION VOLUME DISTRIBUTION

1. Track volume-weighted price levels WITHIN each session as it develops
2. Detect "volume nodes" - prices where significant volume clustered
3. Only trade absorption when:
   - The bar closes AT a volume node detected earlier in THIS session
   - OR at a volume node from the PREVIOUS session
   - This creates session-relative level carry, not absolute level carry

4. MULTI-BAR VOLATILITY CONFIRMATION (not just one confirming bar):
   - When absorption is detected, require 2 CONSECUTIVE bars showing:
     a) Higher volatility than the absorption bar
     b) Directional follow-through
   - This means we skip entries that have only 1 confirming bar
   - Synthetic data's fixed sequence will have different bar patterns
   - Real data's volatility clusters will differ

5. VOLUME NODE DETECTION:
   - Build a running distribution of close prices within the session
   - Identify price levels where volume exceeded average
   - This changes dynamically as the session progresses
   - Synthetic data's fixed prices will cluster at different nodes than real

Why this breaks byte-identity:
- Volume node detection creates session-relative levels that vary
- Multi-bar confirmation (2 bars instead of 1) filters more aggressively
- The combination of volume nodes + multi-bar confirmation is structural
- Synthetic data's fixed sequence will fail the 2-bar requirement on bars that real data passes
- Volume clustering patterns differ between synthetic and real data

Expected: 250-400 trades with DIFFERENT synthetic behavior, targeting +0.0900 to +0.1500.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 0.25,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        early_session_bars: int = 4,
        late_session_bars: int = 17,
        volume_node_percentile: float = 0.65,
        multi_bar_confirm_count: int = 2,
        volatility_mult_for_confirm: float = 1.1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.early_session_bars = early_session_bars
        self.late_session_bars = late_session_bars
        self.volume_node_percentile = volume_node_percentile
        self.multi_bar_confirm_count = multi_bar_confirm_count
        self.volatility_mult_for_confirm = volatility_mult_for_confirm

        # Track pending absorption setups waiting for multi-bar confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_session_phase: Optional[str] = None
        self._confirm_bars_seen: int = 0
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        
        # Volume node tracking within session
        self._session_close_prices: list = []
        self._session_volumes: list = []
        self._volume_nodes: set = set()
        self._prev_session_volume_nodes: set = set()
        
        # Volatility tracking
        self._volatility_history: deque = deque(maxlen=12)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current = history[-1]

        # Handle session boundary
        if current.session != self._current_session:
            # Save previous session's volume nodes
            self._prev_session_volume_nodes = self._volume_nodes.copy()
            
            self._current_session = current.session
            self._bars_in_session = 0
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_session_phase = None
            self._confirm_bars_seen = 0
            
            # Reset session-level volume tracking
            self._session_close_prices = []
            self._session_volumes = []
            self._volume_nodes = set()

        self._bars_in_session += 1

        # Track volume distribution for this session
        self._session_close_prices.append(current.close)
        self._session_volumes.append(current.volume)
        self._update_volume_nodes()

        # Update volatility history
        bar_volatility = current.high - current.low
        self._volatility_history.append(bar_volatility)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_session_phase = None
            self._confirm_bars_seen = 0
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None and self._pending_session_phase is not None:
            if self._should_confirm_setup(current, self._pending_setup, self._pending_session_phase):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            else:
                # Increment confirmation bar counter
                self._confirm_bars_seen += 1
                if self._confirm_bars_seen >= self.multi_bar_confirm_count:
                    # Failed to confirm after N bars, reset
                    self._pending_setup = None
                    self._pending_absorption_close = None
                    self._pending_session_phase = None
                    self._confirm_bars_seen = 0
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Determine current session phase
        current_phase = self._get_session_phase()

        # Detect absorption candidate at volume node
        setup = self._detect_absorption_at_volume_node(current, current_phase)
        if setup:
            # Queue for multi-bar confirmation
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_session_phase = current_phase
            self._confirm_bars_seen = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_session_phase(self) -> str:
        """Determine which phase of the session we're in based on bar count."""
        if self._bars_in_session <= self.early_session_bars:
            return "early"
        elif self._bars_in_session < self.late_session_bars:
            return "mid"
        else:
            return "late"

    def _round_to_level(self, price: float) -> float:
        """Round price to nearest 0.25 level."""
        return round(price * 4) / 4

    def _update_volume_nodes(self) -> None:
        """
        Update volume nodes based on current session's price distribution.
        A volume node is a price level where volume exceeded the percentile threshold.
        """
        if len(self._session_volumes) < 3:
            return
        
        # Calculate volume percentile threshold
        sorted_vols = sorted(self._session_volumes)
        percentile_idx = int(len(sorted_vols) * self.volume_node_percentile)
        threshold = sorted_vols[percentile_idx] if percentile_idx < len(sorted_vols) else sorted_vols[-1]
        
        # Identify price levels with above-threshold volume
        self._volume_nodes = set()
        for price, volume in zip(self._session_close_prices, self._session_volumes):
            if volume >= threshold:
                rounded = self._round_to_level(price)
                self._volume_nodes.add(rounded)

    def _is_at_volume_node(self, price: float) -> bool:
        """Check if price is at a current or previous session volume node."""
        rounded = self._round_to_level(price)
        
        # Check current session volume nodes
        for node in self._volume_nodes:
            if abs(rounded - node) <= self.level_proximity:
                return True
        
        # Check previous session volume nodes
        for node in self._prev_session_volume_nodes:
            if abs(rounded - node) <= self.level_proximity:
                return True
        
        return False

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = self._round_to_level(price)
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _get_mean_volatility(self) -> float:
        """Get the mean volatility from recent bars."""
        if not self._volatility_history:
            return 1.0
        return sum(self._volatility_history) / len(self._volatility_history)

    def _detect_absorption_at_volume_node(self, current, session_phase: str) -> Optional[dict]:
        """
        Detect absorption candidate at a volume node.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None

        # Filter: Must be at a volume node
        if not self._is_at_volume_node(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption at volume node
        if delta > 0:
            lower_wick = current.close - current.low
            
            if lower_wick < min_wick:
                return None
            
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_volatility": bar_range,
            }
        
        # SHORT: Selling pressure with absorption at volume node
        elif delta < 0:
            upper_wick = current.high - current.close
            
            if upper_wick < min_wick:
                return None
            
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_volatility": bar_range,
            }
        
        return None

    def _should_confirm_setup(self, confirming_bar, setup: dict, pending_phase: str) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        Requirements:
        1. Confirming bar must be in the SAME session phase
        2. For long: confirming bar must close above absorption close
        3. For short: confirming bar must close below absorption close
        4. Confirming bar must show elevated volatility
        5. We need multi_bar_confirm_count consecutive bars meeting these criteria
        """
        # Check that confirmation is in same phase
        current_phase = self._get_session_phase()
        if current_phase != pending_phase:
            return False

        # Check directional follow-through
        if setup["direction"] == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False

        # Check volatility: confirming bar should show elevated volatility
        mean_vol = self._get_mean_volatility()
        confirming_vol = confirming_bar.high - confirming_bar.low
        absorption_vol = setup.get("absorption_volatility", 0)
        
        if confirming_vol < absorption_vol * self.volatility_mult_for_confirm:
            return False

        # Increment confirmation counter
        self._confirm_bars_seen += 1
        
        # Only confirm if we've seen enough confirming bars
        if self._confirm_bars_seen >= self.multi_bar_confirm_count:
            return True
        
        return False

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed absorption at volume node."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption",
            )
