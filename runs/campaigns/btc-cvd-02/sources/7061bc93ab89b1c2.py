"""Cumulative volume delta continuation on the OKX BTC perpetual.

The claim under test: unusually large aggressive buying is followed by higher
prices and unusually large aggressive selling by lower prices, over the next
comparable window. This file tests continuation, in the direction of the flow,
at a horizon equal to the measurement window. It does not fade.

Signal
------
`bar.delta` is buy_volume - sell_volume, so the sum of `bar.delta` over the last
`flow_bars` is cumulative volume delta over that window. CVD in contracts is not
comparable across hours and years, so it is divided by the total volume traded
over the same window: a flow imbalance ratio in roughly [-1, 1] that says what
fraction of the window's volume was one-sided. That number is then z-scored
against its own rolling distribution (one day of 5m bars), so a "large" reading
means large relative to what this instrument has recently been doing rather than
large against a constant picked in advance. Both statistics are kept as running
sums, so each bar costs O(1) rather than a rescan.

Entry, exit, holding
--------------------
Enter with a market order, in the direction of the imbalance, when both the
z-score and the raw ratio are extreme. A market order is deliberate: a resting
limit fills here only when trades come to it (book-queue-pessimistic), which on
a continuation signal selects exactly the cases where the move did not happen.
Exit on the clock after `hold_bars` -- the horizon is part of the hypothesis, so
it is fixed rather than adaptive -- with a wide OCO bracket armed by the engine
at the instant the entry fills. The bracket is tail protection at twice the
horizon's typical range, not a competing exit; the time exit is expected to fire
first, and it cancels the bracket legs before flattening so they cannot linger
as working orders.

Costs
-----
5bp per side, so a round trip must earn 10bp before it earns anything. That rules
out high turnover: one position at a time, a cooldown of six bars after each
exit, and no new entry within fifteen minutes of a funding settlement. The
horizon is chosen so the drift, not the tick, has to pay the fee.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Deque, Tuple

_NS_PER_SECOND = 1_000_000_000
_FUNDING_SECONDS = (0, 8 * 3600, 16 * 3600)


class Strategy:
    """CVD continuation: trade with extreme one-hour order-flow imbalance."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        flow_bars: int = 12,          # one hour of 5m bars
        norm_bars: int = 288,         # one day of 5m bars of z-score history
        min_norm: int = 96,           # require 8 hours of history before trading
        z_entry: float = 2.0,
        min_abs_ratio: float = 0.05,
        hold_bars: int = 12,          # one hour, matching the measurement window
        cooldown_bars: int = 6,
        stop_pct: float = 0.02,
        target_pct: float = 0.02,
        size: int = 10,
        funding_margin: int = 900,    # seconds before funding with no new entries
    ) -> None:
        self.interval = interval
        self.flow_bars = flow_bars
        self.norm_bars = norm_bars
        self.min_norm = min_norm
        self.z_entry = z_entry
        self.min_abs_ratio = min_abs_ratio
        self.hold_bars = hold_bars
        self.cooldown_bars = cooldown_bars
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.size = size
        self.funding_margin = funding_margin

        # Rolling flow window: (delta, volume) per 5m bar, with running sums.
        self.flow: Deque[Tuple[float, float]] = deque()
        self.sum_delta = 0.0
        self.sum_vol = 0.0

        # Rolling distribution of the imbalance ratio, with running moments.
        self.ratios: Deque[float] = deque()
        self.sum_r = 0.0
        self.sum_r2 = 0.0

        self.bars_held = 0
        self.exiting = False
        self.cooldown = 0

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------ bars

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # Features accumulate in every session, warmup included: orders are
        # ignored during warmup but the distribution still needs to exist by
        # the first actionable bar.
        self._push_flow(float(bar.delta), float(bar.volume))
        if len(self.flow) < self.flow_bars or self.sum_vol <= 0.0:
            return
        ratio = self.sum_delta / self.sum_vol
        z = self._zscore(ratio)
        self._push_ratio(ratio)

        if not ctx.actionable:
            return

        if self.cooldown > 0:
            self.cooldown -= 1

        # --- manage an open position: the clock is the exit -------------------
        if ctx.position != 0:
            self.bars_held += 1
            if self.bars_held >= self.hold_bars and not self.exiting:
                self.exiting = True
                # Cancel the bracket legs first so they cannot linger as
                # working orders once the position is gone, then flatten.
                ctx.cancel_all()
                ctx.close()
            return

        # --- flat -------------------------------------------------------------
        self.bars_held = 0
        self.exiting = False

        if ctx.working_order_ids:
            # Leftovers from an earlier bracket. Clear them once the cooldown
            # is spent rather than trading around them.
            if self.cooldown == 0:
                ctx.cancel_all()
            return

        if z is None or self.cooldown > 0:
            return
        if abs(ratio) < self.min_abs_ratio:
            return
        if self._near_funding(ctx.ts):
            return

        px = ctx.last_price
        if px is None or px <= 0.0:
            return

        stop_off = px * self.stop_pct
        target_off = px * self.target_pct

        if z >= self.z_entry:
            ctx.buy(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="cvd-long",
            )
        elif z <= -self.z_entry:
            ctx.sell(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="cvd-short",
            )

    # ------------------------------------------------------------------ fills

    def on_fill(self, ctx) -> None:
        if ctx.fill.position == 0:
            # The book was flattened -- by the clock, the stop or the target.
            if self.bars_held > 0 or self.exiting:
                self.cooldown = self.cooldown_bars
            self.bars_held = 0
            self.exiting = False
        else:
            # Newly open: the holding clock starts here, not at the order.
            self.bars_held = 0
            self.exiting = False

    def on_lifecycle(self, ctx) -> None:
        pass

    # ---------------------------------------------------------------- helpers

    def _push_flow(self, delta: float, volume: float) -> None:
        if len(self.flow) >= self.flow_bars:
            old_delta, old_vol = self.flow.popleft()
            self.sum_delta -= old_delta
            self.sum_vol -= old_vol
        self.flow.append((delta, volume))
        self.sum_delta += delta
        self.sum_vol += volume

    def _push_ratio(self, ratio: float) -> None:
        if len(self.ratios) >= self.norm_bars:
            old = self.ratios.popleft()
            self.sum_r -= old
            self.sum_r2 -= old * old
        self.ratios.append(ratio)
        self.sum_r += ratio
        self.sum_r2 += ratio * ratio

    def _zscore(self, ratio: float) -> float | None:
        n = len(self.ratios)
        if n < self.min_norm:
            return None
        mean = self.sum_r / n
        var = self.sum_r2 / n - mean * mean
        if var <= 0.0:
            return None
        return (ratio - mean) / math.sqrt(var)

    def _near_funding(self, ts_ns: int) -> bool:
        second_of_day = (ts_ns // _NS_PER_SECOND) % 86400
        for f in _FUNDING_SECONDS:
            until = (f - second_of_day) % 86400
            if until < self.funding_margin:
                return True
        return False
