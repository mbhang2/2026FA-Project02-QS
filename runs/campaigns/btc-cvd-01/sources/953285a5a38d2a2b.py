"""Cumulative volume delta continuation on the BTC perpetual.

The campaign question: does aggressive flow continue? Over a window, add up
buyer-initiated volume and subtract seller-initiated volume; the claim is that an
unusually large positive imbalance is followed by higher prices over the next
comparable window, and a negative one by lower prices. This file is a direct
test of that, in that direction, with nothing faded.

Choices, and why they are the ones made here:

- **5m bars, no tick subscription.** Each bar already carries `buy_volume` and
  `sell_volume`, so `bar.delta` is the signed aggressor flow over the interval
  without paying the ~5x wall-clock cost of a tick subscription, and without
  needing the `side is None` branch (this feed attributes every trade).

- **Volume-weighted imbalance, not a raw contract count.** The signal is
  `sum(delta) / sum(volume)` over the window, which lives in [-1, 1] and is
  therefore already regime-free: an hour of one-sided flow on thin volume is not
  the same statement as the same flow on heavy volume. "Large" is then made
  explicitly adaptive by z-scoring that ratio against its own trailing
  distribution over `baseline` windows, so the threshold means the same thing in
  a quiet 2023 session and a violent 2026 one. Both the flow sums and the ratio
  moments are maintained incrementally (O(1) per bar), with a periodic exact
  recompute against float drift, because recomputing over the whole history on
  every bar is the standard way this kind of run dies against the budget.

- **Hysteresis.** After an entry the signal must fall back inside `reset_z`
  before another entry can fire. One flow episode is one trade, not twenty
  overlapping ones; without this the entry signal, whose window overlaps itself,
  would re-fire every bar and the commission alone would decide the result.

- **Exits.** The claim names a horizon ("the next comparable window"), so the
  position is closed at market after `hold_bars` bars, with the horizon set equal
  to the measurement window. A wide protective stop (2%) and a far target (8%)
  are carried as a bracket so the position is protected from the instant it
  fills; they are deliberately wide enough that they are tail insurance rather
  than a second, path-dependent exit rule competing with the one being tested.
  Working orders are cancelled before the time exit, so a dead bracket cannot
  strand a reduce-only order that would then block the entry gate.

- **Costs.** 5bp a side plus the touch, so ~10bp round trip before anything else.
  Nothing here trades often: a handful of entries a session at most, held for
  hours. A flow effect that only pays before fees is not an effect here, and the
  design refuses to hide that by churning.

- Market orders on entry and exit. A resting limit on the bid would only fill
  when the price ticks through it, which for a continuation signal is exactly the
  adverse selection the pessimistic book model is there to charge for.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Buy unusually strong aggressive buying, sell unusually strong selling."""

    def __init__(
        self,
        interval: str = "5m",
        window: int = 36,
        baseline: int = 192,
        min_baseline: int = 96,
        entry_z: float = 2.0,
        reset_z: float = 0.75,
        hold_bars: int = 36,
        size: int = 1,
        stop_frac: float = 0.02,
        target_frac: float = 0.08,
    ) -> None:
        self.interval = interval
        self.window = window
        self.baseline = baseline
        self.min_baseline = min_baseline
        self.entry_z = entry_z
        self.reset_z = reset_z
        self.hold_bars = hold_bars
        self.size = size
        self.stop_frac = stop_frac
        self.target_frac = target_frac

        # Rolling flow sums over the measurement window.
        self._d: deque = deque(maxlen=window)
        self._v: deque = deque(maxlen=window)
        self._sum_d = 0.0
        self._sum_v = 0.0

        # Trailing distribution of the imbalance ratio.
        self._ratios: deque = deque(maxlen=baseline)
        self._r_sum = 0.0
        self._r_sq = 0.0
        self._since_refresh = 0

        # Trade state.
        self._armed = True
        self._bars_held = 0
        self._exiting = False

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------- internals

    def _push_flow(self, delta: float, volume: float) -> None:
        if len(self._d) == self.window:
            self._sum_d -= self._d[0]
            self._sum_v -= self._v[0]
        self._d.append(delta)
        self._v.append(volume)
        self._sum_d += delta
        self._sum_v += volume

    def _push_ratio(self, ratio: float) -> None:
        if len(self._ratios) == self.baseline:
            old = self._ratios[0]
            self._r_sum -= old
            self._r_sq -= old * old
        self._ratios.append(ratio)
        self._r_sum += ratio
        self._r_sq += ratio * ratio
        self._since_refresh += 1
        if self._since_refresh >= 1024:
            self._since_refresh = 0
            self._r_sum = sum(self._ratios)
            self._r_sq = sum(r * r for r in self._ratios)

    def _z(self):
        n = len(self._ratios)
        if n < self.min_baseline:
            return None
        mean = self._r_sum / n
        var = self._r_sq / n - mean * mean
        if var <= 0.0:
            return None
        sd = var ** 0.5
        if sd <= 1e-12:
            return None
        return (self._ratios[-1] - mean) / sd

    def _enter(self, ctx, direction: int) -> None:
        price = float(ctx.last_price)
        if price <= 0.0:
            return
        stop = price * self.stop_frac
        target = price * self.target_frac
        if stop <= 0.0 or target <= 0.0:
            return
        if direction > 0:
            oid = ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tif="GTC",
                tag="cvd-entry",
            )
        else:
            oid = ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tif="GTC",
                tag="cvd-entry",
            )
        if oid is None:
            return
        self._armed = False
        self._bars_held = 0
        self._exiting = False

    # ------------------------------------------------------------- callbacks

    def on_bars(self, ctx) -> None:
        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        self._push_flow(float(bar.delta), float(bar.volume))
        if len(self._d) < self.window or self._sum_v <= 0.0:
            return
        self._push_ratio(self._sum_d / self._sum_v)

        if not ctx.actionable:
            return

        if ctx.position != 0:
            self._bars_held += 1
            if not self._exiting and self._bars_held >= self.hold_bars:
                self._exiting = True
                ctx.cancel_all()
                ctx.close()
            return

        # Flat: reset the hold counter and consider a new entry.
        self._exiting = False
        self._bars_held = 0

        if ctx.working_order_ids:
            return

        z = self._z()
        if z is None:
            return
        if abs(z) < self.reset_z:
            self._armed = True
        if not self._armed:
            return
        if z >= self.entry_z:
            self._enter(ctx, 1)
        elif z <= -self.entry_z:
            self._enter(ctx, -1)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
