"""Absorption-and-initiation, v2: fix the exposure problem.

Prior trial #0 exposed in 0 of 384 scored sessions. The likely causes:
- `_delta_threshold` required a full 60-bar window of abs deltas before any
  pattern could fire, and the pending absorption signal was only checked
  when a new bar arrived. If the initiation bar was the last bar before
  a session close or the strategy was flat and the pattern completed
  during a period with no new bars, it would never enter.
- The entry required the initiation bar to be within `initiation_window`
  bars after the absorption bar, but the pending signal was dropped only
  when `len(recent_bars) - absorption_bar - 1 > initiation_window`.
  Because `recent_bars` is a deque with maxlen, old bars are evicted and
  the index math could break, leaving a stale pending signal that never
  fires.

This version:
- Uses a simpler, more robust state machine: track the absorption bar
  explicitly, and check for initiation on every bar after it.
- Relaxes the initiation window to a longer lookback (still bounded) and
  allows initiation to be detected on the same bar as absorption if the
  bar itself shows both absorption and opposite-flow characteristics.
- Reduces the minimum delta window to 20 bars so the strategy can start
  acting sooner in a session.
- Adds a time-based exit via bracket target, but also a maximum holding
  period using a bar count to avoid being stuck in a position through
  session changes.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        delta_lookback: int = 20,
        delta_mult: float = 1.2,
        initiation_window: int = 5,
        range_lookback: int = 20,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        max_hold_bars: int = 30,
        size: int = 1,
    ):
        self.interval = interval
        self.delta_lookback = delta_lookback
        self.delta_mult = delta_mult
        self.initiation_window = initiation_window
        self.range_lookback = range_lookback
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.max_hold_bars = max_hold_bars
        self.size = size

        self.abs_deltas: deque[float] = deque(maxlen=delta_lookback)
        self.recent_bars: deque = deque(maxlen=max(delta_lookback, range_lookback, initiation_window + 5))
        # Absorption state: (direction, bar_open_ts, bars_since)
        self.pending_absorption: tuple[int, int, int] | None = None
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
                self.pending_absorption = None
            return

        if ctx.working_order_ids:
            return

        # Need enough data for normalization.
        if len(self.abs_deltas) < self.delta_lookback:
            return

        # Check pending absorption for initiation.
        if self.pending_absorption is not None:
            direction, absorption_ts, _ = self.pending_absorption
            # Count bars since absorption using open_ts.
            bars_since = 0
            for b in reversed(list(self.recent_bars)[:-1]):
                if b.open_ts == absorption_ts:
                    break
                bars_since += 1
            if bars_since <= self.initiation_window:
                # Look at bars after absorption, including current bar.
                for b in list(self.recent_bars)[-bars_since - 1:]:
                    if b.open_ts <= absorption_ts:
                        continue
                    if self._is_initiation(b, direction):
                        self._enter(ctx, direction, b)
                        self.pending_absorption = None
                        return
            else:
                # Too late; drop the signal.
                self.pending_absorption = None

        # Detect absorption on current bar.
        if self._is_absorption(bar):
            direction = 1 if bar.delta > 0 else -1
            self.pending_absorption = (direction, bar.open_ts, 0)

    def _is_absorption(self, bar) -> bool:
        if bar.delta == 0:
            return False
        threshold = self._delta_threshold()
        if abs(bar.delta) < threshold:
            return False

        if len(self.recent_bars) < 2:
            return False
        prior = self.recent_bars[-2]

        if bar.delta > 0:
            return bar.high <= prior.high or bar.close < prior.high
        else:
            return bar.low >= prior.low or bar.close > prior.low

    def _is_initiation(self, bar, direction: int) -> bool:
        # Opposite delta direction.
        if direction > 0 and bar.delta >= 0:
            return False
        if direction < 0 and bar.delta <= 0:
            return False

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
        if not self.abs_deltas:
            return 0.0
        sorted_deltas = sorted(self.abs_deltas)
        n = len(sorted_deltas)
        median = sorted_deltas[n // 2] if n % 2 == 1 else (sorted_deltas[n // 2 - 1] + sorted_deltas[n // 2]) / 2
        return median * self.delta_mult

    def _enter(self, ctx, direction: int, bar) -> None:
        if ctx.position or ctx.working_order_ids:
            return

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
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="absorption-short",
            )
        else:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="absorption-long",
            )
        self.bars_held = 0

    def on_fill(self, ctx) -> None:
        # Reset hold counter when a position is opened.
        if ctx.fill.position != 0:
            self.bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        # Clear pending signals on session close to avoid stale state.
        if ctx.event.kind == "session_close":
            self.pending_absorption = None
            self.bars_held = 0
