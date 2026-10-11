"""Absorption-and-initiation, v3: pattern detection on 5m bars with state kept on self.

Prior trials exposed in very few sessions because the 1m pattern was too strict
and the state machine was fragile. This version:

- Uses 5-minute bars, which are less noisy and give the pattern more room.
- Keeps the rolling delta window and recent bars on self, not relying on
  ctx.history beyond the current bar.
- Detects absorption and initiation on the same bar close, then enters
  immediately with a bracket. No pending state that can go stale.
- Requires a minimum history length before trading.
- Uses a fixed delta threshold based on the rolling median absolute delta,
  but with a floor to avoid zero thresholds early on.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        delta_lookback: int = 40,
        delta_mult: float = 1.5,
        range_lookback: int = 20,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.delta_lookback = delta_lookback
        self.delta_mult = delta_mult
        self.range_lookback = range_lookback
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size

        self.abs_deltas: deque[float] = deque(maxlen=delta_lookback)
        self.recent_bars: deque = deque(maxlen=max(delta_lookback, range_lookback) + 2)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # One position at a time; bracket handles exit.
        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        bar = history[-1]
        self.recent_bars.append(bar)
        self.abs_deltas.append(abs(bar.delta))

        # Need enough data for normalization and range.
        if len(self.abs_deltas) < self.delta_lookback:
            return
        if len(self.recent_bars) < self.range_lookback + 2:
            return

        # Check for absorption on the current bar.
        if not self._is_absorption(bar):
            return

        # Absorption detected. Look for initiation on the same bar or the
        # immediately preceding bar (opposite efficient flow).
        direction = 1 if bar.delta > 0 else -1
        initiation_bar = None

        # Check current bar for initiation (opposite direction efficient flow).
        if self._is_initiation(bar, direction):
            initiation_bar = bar
        else:
            # Check the prior bar.
            if len(self.recent_bars) >= 2:
                prior = self.recent_bars[-2]
                if self._is_initiation(prior, direction):
                    initiation_bar = prior

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
            # Buying absorbed: fails to make a new high or closes below prior high.
            return bar.high <= prior.high or bar.close < prior.high
        else:
            # Selling absorbed: fails to make a new low or closes above prior low.
            return bar.low >= prior.low or bar.close > prior.low

    def _is_initiation(self, bar, direction: int) -> bool:
        """Efficient opposite flow."""
        # Opposite delta direction.
        if direction > 0 and bar.delta >= 0:
            return False
        if direction < 0 and bar.delta <= 0:
            return False

        # Large in magnitude.
        if abs(bar.delta) < self._delta_threshold():
            return False

        # Closes near its extreme in the direction of its delta.
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return False
        if bar.delta < 0 and bar.close > bar.low + 0.3 * bar_range:
            return False
        if bar.delta > 0 and bar.close < bar.high - 0.3 * bar_range:
            return False

        return True

    def _delta_threshold(self) -> float:
        """Median absolute delta times a multiplier, with a small floor."""
        if not self.abs_deltas:
            return 0.0
        sorted_deltas = sorted(self.abs_deltas)
        n = len(sorted_deltas)
        median = sorted_deltas[n // 2] if n % 2 == 1 else (sorted_deltas[n // 2 - 1] + sorted_deltas[n // 2]) / 2
        # Floor to avoid a zero threshold when many zero-delta bars exist.
        return max(median * self.delta_mult, 1.0)

    def _enter(self, ctx, direction: int) -> None:
        """Enter in the direction opposite the absorbed flow."""
        if ctx.position or ctx.working_order_ids:
            return

        # Range for bracket sizing.
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

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        # Clear rolling state on session close to avoid cross-session leakage.
        if ctx.event.kind == "session_close":
            self.abs_deltas.clear()
            self.recent_bars.clear()
