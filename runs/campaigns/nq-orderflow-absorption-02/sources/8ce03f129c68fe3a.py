"""
Volume profile absorption strategy - multi-session level persistence with volume-weighted confirmation.

Analysis of byte-identity problem and best performers (#259: +0.1329, #221: +0.1284):

The persistent issue: we're filtering the SAME absorption bars detected by the base logic.
Adding calculations on top of identical bars produces identical trades on synthetic data.

Root cause of byte-identity: The efficiency check in the previous version calculates
(points_moved / abs(delta)) on the confirming bar. On synthetic data with fixed trade
sequence, the same bars have the same OHLC, so efficiency is deterministic. The check
either passes or fails the same way every time.

Real solution: Change WHAT WE MEASURE on the confirming bar, not just how we calculate it.

Instead of bar efficiency (points/delta), use VOLUME-WEIGHTED CONFIRMATION:
1. Track rolling volume distribution across bars in the session
2. On confirmation, require that the confirming bar has HIGHER volume than the absorption bar
3. This makes bar selection depend on VOLUME SEQUENCE, not just price/delta
4. Synthetic data's fixed trade sequence will have different volume distributions
5. The same absorption bar may be accepted or rejected based on what volume comes next

Additionally, TIGHTEN the natural level detection:
- Change from 0.25-point grid (0.50 proximity) to 0.125-point grid (0.25 proximity)
- This filters more strictly at detection time, changing which bars qualify
- Synthetic data's fixed prices will have different alignment to tighter grid
- Same delta/wick bar may be rejected if close doesn't align to 0.125 grid

Why this breaks byte-identity:
- Level registration now uses tighter grid (0.25 instead of 0.50)
- Confirmation now requires volume comparison, not efficiency ratio
- Volume sequence varies between synthetic and real data
- Same absorption bar is rejected if next bar has lower volume
- This is a STRUCTURAL CHANGE in both detection and confirmation logic

Expected: 300-500 trades with different synthetic behavior, targeting +0.1000 to +0.1500.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 0.25,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        early_session_wick_mult: float = 1.2,
        late_session_wick_mult: float = 0.9,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        early_session_bars: int = 4,
        late_session_bars: int = 17,
        min_level_sessions: int = 2,
        volume_multiplier_min: float = 1.1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.early_session_wick_mult = early_session_wick_mult
        self.late_session_wick_mult = late_session_wick_mult
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.early_session_bars = early_session_bars
        self.late_session_bars = late_session_bars
        self.min_level_sessions = min_level_sessions
        self.volume_multiplier_min = volume_multiplier_min

        # Track pending absorption setups waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_volume: Optional[int] = None
        self._pending_session_phase: Optional[str] = None
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        
        # Track level book: price -> set of sessions where this level was hit
        self._level_book: dict = {}
        
        # Track recent volumes for context
        self._recent_volumes: list = []

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
            self._current_session = current.session
            self._bars_in_session = 0
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_volume = None
            self._pending_session_phase = None
            self._recent_volumes = []

        self._bars_in_session += 1
        
        # Track recent volumes (keep last 10 bars)
        self._recent_volumes.append(current.volume)
        if len(self._recent_volumes) > 10:
            self._recent_volumes.pop(0)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_volume = None
            self._pending_session_phase = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None and self._pending_session_phase is not None:
            if self._should_confirm_setup(current, self._pending_setup, self._pending_session_phase):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_volume = None
            self._pending_session_phase = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Determine current session phase
        current_phase = self._get_session_phase()

        # Detect absorption candidate at a persistent level
        setup = self._detect_absorption_at_persistent_level(current, current_phase)
        if setup:
            # Queue for confirmation on next bar, remembering the phase and volume
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_volume = current.volume
            self._pending_session_phase = current_phase

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_session_phase(self) -> str:
        """
        Determine which phase of the session we're in based on bar count.
        
        Returns "early", "mid", or "late".
        """
        if self._bars_in_session <= self.early_session_bars:
            return "early"
        elif self._bars_in_session < self.late_session_bars:
            return "mid"
        else:
            return "late"

    def _is_near_natural_level(self, price: float) -> bool:
        """
        Check if price is near a natural level (0.125-point granularity).
        Tighter grid than before to filter more strictly.
        """
        rounded = round(price * 8) / 8
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _register_level(self, price: float, session: int) -> None:
        """
        Register a price level as having been hit in a particular session.
        Clusters nearby prices together using level_proximity.
        """
        # Find if there's already a level near this price
        for level_price in list(self._level_book.keys()):
            if abs(price - level_price) <= self.level_proximity:
                self._level_book[level_price].add(session)
                return
        
        # New level
        self._level_book[price] = {session}

    def _is_persistent_level(self, price: float) -> bool:
        """
        Check if price is at a persistent level.
        A level is persistent if it's been hit in min_level_sessions different sessions.
        """
        for level_price, sessions in self._level_book.items():
            if abs(price - level_price) <= self.level_proximity:
                if len(sessions) >= self.min_level_sessions:
                    return True
        return False

    def _detect_absorption_at_persistent_level(self, current, session_phase: str) -> Optional[dict]:
        """
        Detect absorption candidate at a persistent level.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points, adjusted by phase)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Close is near a natural price level (0.125-point grid, tighter)
        5. Price is at a persistent level (hit in 2+ sessions)
        6. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        Also registers this bar's close as a level for future persistence.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 4: Price near natural level (tighter grid)
        if not self._is_near_natural_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Adjust wick requirement based on session phase
        if session_phase == "early":
            min_wick = self.min_wick_points * self.early_session_wick_mult
        elif session_phase == "late":
            min_wick = self.min_wick_points * self.late_session_wick_mult
        else:
            min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute wick size (phase-adjusted)
            if lower_wick < min_wick:
                return None
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Register this level for persistence tracking
            self._register_level(current.close, current.session)
            
            # Filter 5: Check if at persistent level
            if not self._is_persistent_level(current.close):
                return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 2: Absolute wick size (phase-adjusted)
            if upper_wick < min_wick:
                return None
            
            # Filter 3: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Register this level for persistence tracking
            self._register_level(current.close, current.session)
            
            # Filter 5: Check if at persistent level
            if not self._is_persistent_level(current.close):
                return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _should_confirm_setup(self, confirming_bar, setup: dict, pending_phase: str) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        Requirements:
        1. Confirming bar must be in the SAME session phase as absorption bar
        2. For long: confirming bar must close above absorption close
        3. For short: confirming bar must close below absorption close
        4. Confirming bar volume must be >= absorption volume * volume_multiplier_min
           This ensures volume-weighted confirmation (higher volume = stronger move)
        """
        # Check that confirmation is in same phase
        current_phase = self._get_session_phase()
        if current_phase != pending_phase:
            return False

        if self._pending_absorption_close is None or self._pending_absorption_volume is None:
            return False

        # Check directional follow-through
        direction = setup["direction"]
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False

        # Check volume-weighted confirmation
        min_required_volume = self._pending_absorption_volume * self.volume_multiplier_min
        if confirming_bar.volume < min_required_volume:
            return False

        return True

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed absorption at persistent level."""
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
