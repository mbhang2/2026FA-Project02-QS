"""Absorption-and-initiation on 1-minute bars.

The question: does a bar with large delta that fails to move price mark a turn,
especially when followed by efficient opposite flow?

This implementation:
- Uses 1-minute bars for the pattern detection.
- Maintains a rolling window of delta magnitudes to normalize "large".
- Absorption: |delta| >= k * rolling median absolute delta, and the bar closes
  against the delta direction relative to the prior bar's high/low.
- Initiation: within the next `initiation_window` bars, a bar with opposite
  delta that is also large and closes near its extreme in the direction of its
  delta.
- Enters on the close of the initiation bar, with a bracket sized from recent
  range.
- Exits via the bracket; no time-based exit to keep the pattern pure.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        delta_lookback: int = 60,
        delta_mult: float = 1.5,
        initiation_window: int = 3,
        range_lookback: int = 20,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.delta_lookback = delta_lookback
        self.delta_mult = delta_mult
        self.initiation_window = initiation_window
        self.range_lookback = range_lookback
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size

        # Rolling window of absolute delta values for normalization.
        self.abs_deltas: deque[float] = deque(maxlen=delta_lookback)
        # Recent bars for range estimation and pattern detection.
        self.recent_bars: deque = deque(maxlen=max(delta_lookback, range_lookback, initiation_window + 2))
        # Pending absorption signal: (direction, bar_index_in_recent)
        self.pending_absorption: tuple[int, int] | None = None

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

        # Need enough data for normalization and range.
        if len(self.abs_deltas) < self.delta_lookback:
            return

        # Check for pending absorption followed by initiation.
        if self.pending_absorption is not None:
            direction, absorption_bar = self.pending_absorption
            # Look at bars after the absorption bar.
            for b in list(self.recent_bars)[absorption_bar + 1:]:
                if self._is_initiation(b, direction):
                    self._enter(ctx, direction, b)
                    self.pending_absorption = None
                    return
            # If too many bars have passed, drop the pending signal.
            if len(self.recent_bars) - absorption_bar - 1 > self.initiation_window:
                self.pending_absorption = None

        # Detect absorption on the current bar.
        if self._is_absorption(bar):
            # Store direction: +1 means absorption of buying (expect reversal down),
            # -1 means absorption of selling (expect reversal up).
            direction = 1 if bar.delta > 0 else -1
            self.pending_absorption = (direction, len(self.recent_bars) - 1)

    def _is_absorption(self, bar) -> bool:
        """Large delta but price fails to move with it."""
        if bar.delta == 0:
            return False
        threshold = self._delta_threshold()
        if abs(bar.delta) < threshold:
            return False

        # Need a prior bar to compare high/low.
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
        if bar.delta < 0 and bar.close > bar.low + 0.25 * (bar.high - bar.low):
            return False
        if bar.delta > 0 and bar.close < bar.high - 0.25 * (bar.high - bar.low):
            return False

        return True

    def _delta_threshold(self) -> float:
        """Median absolute delta times a multiplier."""
        if not self.abs_deltas:
            return 0.0
        sorted_deltas = sorted(self.abs_deltas)
        n = len(sorted_deltas)
        median = sorted_deltas[n // 2] if n % 2 == 1 else (sorted_deltas[n // 2 - 1] + sorted_deltas[n // 2]) / 2
        return median * self.delta_mult

    def _enter(self, ctx, direction: int, bar) -> None:
        """Enter in the direction opposite the absorbed flow."""
        if ctx.position or ctx.working_order_ids:
            return

        # Range for bracket sizing.
        if len(self.recent_bars) < self.range_lookback:
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

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
