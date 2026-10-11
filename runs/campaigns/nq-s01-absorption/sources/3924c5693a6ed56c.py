"""Absorption-and-initiation, v4: relaxed detection, earlier entries, and time exit.

Prior trial #2 scored -0.0424 with 350 trades over 384 sessions, meaning it
barely trades. The pattern is too strict: requiring absorption and initiation
on the same or immediately preceding 5m bar, with a high delta threshold,
misses most signals.

This version:
- Uses 5m bars but relaxes absorption: large delta relative to rolling median,
  and price fails to make a new high/low versus the prior bar OR closes against
  the delta direction.
- Relaxes initiation: opposite delta direction, above a lower threshold
  (delta_mult reduced), and closes in the bottom/top 40% of its range.
- Allows initiation on the current bar or the prior bar, but also checks the
  bar before that, giving the pattern room to breathe.
- Adds a time-based exit: if the position has not hit target/stop within
  max_hold_bars, close at market. This prevents being stuck in a losing
  position through session changes.
- Keeps all state on self, using deques for rolling windows.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        delta_lookback: int = 30,
        delta_mult: float = 1.2,
        range_lookback: int = 15,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        max_hold_bars: int = 12,
        size: int = 1,
    ):
        self.interval = interval
        self.delta_lookback = delta_lookback
        self.delta_mult = delta_mult
        self.range_lookback = range_lookback
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.max_hold_bars = max_hold_bars
        self.size = size

        self.abs_deltas: deque[float] = deque(maxlen=delta_lookback)
        self.recent_bars: deque = deque(maxlen=max(delta_lookback, range_lookback) + 3)
        self.bars_held = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        bar = history[-1]
        self.recent_bars.append(bar)
        self.abs_deltas.append(abs(bar.delta))

        # Manage existing position: exit if held too long.
        if ctx.position:
            self.bars_held += 1
            if self.bars_held >= self.max_hold_bars:
                ctx.close()
                self.bars_held = 0
            return

        if ctx.working_order_ids:
            return

        # Need enough data for normalization.
        if len(self.abs_deltas) < self.delta_lookback:
            return
        if len(self.recent_bars) < self.range_lookback + 2:
            return

        # Check for absorption on the current bar.
        if not self._is_absorption(bar):
            return

        direction = 1 if bar.delta > 0 else -1
        initiation_bar = None

        # Check current bar and up to two prior bars for initiation.
        candidates = list(self.recent_bars)[-3:]
        for b in reversed(candidates):
            if self._is_initiation(b, direction):
                initiation_bar = b
                break

        if initiation_bar is None:
            return

        self._enter(ctx, direction)

    def _is_absorption(self, bar) -> bool:
        """Large delta but price fails to move with it."""
        if bar.delta == 0:
            return False
        threshold = self._delta_threshold()
        if abs(bar.delta) < threshold:
            return False

        if len(self.recent_bars) < 2:
            return False
        prior = self.recent_bars[-2]

        if bar.delta > 0:
            # Buying absorbed: fails to make a new high, or closes below prior high.
            return bar.high <= prior.high or bar.close < prior.high
        else:
            # Selling absorbed: fails to make a new low, or closes above prior low.
            return bar.low >= prior.low or bar.close > prior.low

    def _is_initiation(self, bar, direction: int) -> bool:
        """Efficient opposite flow."""
        # Opposite delta direction.
        if direction > 0 and bar.delta >= 0:
            return False
        if direction < 0 and bar.delta <= 0:
            return False

        # Lower threshold for initiation - we want to catch the turn early.
        threshold = self._delta_threshold() * 0.8
        if abs(bar.delta) < threshold:
            return False

        # Closes near its extreme in the direction of its delta.
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return False
        if bar.delta < 0 and bar.close > bar.low + 0.4 * bar_range:
            return False
        if bar.delta > 0 and bar.close < bar.high - 0.4 * bar_range:
            return False

        return True

    def _delta_threshold(self) -> float:
        """Median absolute delta times a multiplier, with a small floor."""
        if not self.abs_deltas:
            return 0.0
        sorted_deltas = sorted(self.abs_deltas)
        n = len(sorted_deltas)
        median = sorted_deltas[n // 2] if n % 2 == 1 else (sorted_deltas[n // 2 - 1] + sorted_deltas[n // 2]) / 2
        return max(median * self.delta_mult, 1.0)

    def _enter(self, ctx, direction: int) -> None:
        """Enter in the direction opposite the absorbed flow."""
        if ctx.position or ctx.working_order_ids:
            return

        recent = list(self.recent_bars)[-self.range_lookback:]
        high = max(b.high for b in recent)
        low = min(b.low for b in recent)
        rng = high - low
        if rng <= 0:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if direction > 0:
            # Absorption of buying -> expect down move.
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="absorption-short",
            )
        else:
            # Absorption of selling -> expect up move.
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="absorption-long",
            )
        self.bars_held = 0

    def on_fill(self, ctx) -> None:
        if ctx.fill.position != 0:
            self.bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_close":
            self.abs_deltas.clear()
            self.recent_bars.clear()
            self.bars_held = 0
