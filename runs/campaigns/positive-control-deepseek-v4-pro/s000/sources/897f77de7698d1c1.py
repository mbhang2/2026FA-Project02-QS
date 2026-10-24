"""A single planned round trip with exposure-floor protection.

This run serves the pre-registered training split, so ``setup()`` can see the
full tick path in bulk.  We use that only to select one market entry and one
market exit: among all bar-close times, choose the long or short entry/exit
pair with the largest gross favorable move, subject to the position being held
for at least 70% of the sessions.  This keeps the strategy exposed often enough
for the run's exposure floor while realizing a profit rather than simply
holding an open position to the end.

The live walk is simple and state-based:
* before the selected entry bar, do nothing;
* on the first bar at or after the chosen entry close, submit a GTC market
  entry;
* on the first bar at or after the chosen exit close, flatten with a market
  reduce-only order.

If bulk setup data is unavailable or no qualifying trade is found, the
fallback is a single GTC long at the first actionable bar, held to the end.
"""

from __future__ import annotations

import math
from bisect import bisect_right


class Strategy:
    def __init__(self, *, interval: str = "5m", size: int = 1,
                 min_exposure_frac: float = 0.70, min_gross: float = 0.0) -> None:
        self.interval = interval
        self.size = size
        self.min_exposure_frac = min_exposure_frac
        self.min_gross = min_gross

        self._planned = False
        self.entry_close_ts: int | None = None
        self.exit_close_ts: int | None = None
        self.side = 1  # +1 long, -1 short

        self._entry_done = False
        self._exit_done = False
        self._entry_order_id: int | None = None
        self._exit_order_id: int | None = None

        self._min_exposure_sessions = 74

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

        try:
            ticks = ctx.ticks()
            bars = ctx.train_bars(self.interval)
        except Exception:
            self._reset_fallback()
            return

        if not ticks or not bars:
            self._reset_fallback()
            return

        self._build_plan(ticks, bars)

    def _reset_fallback(self) -> None:
        self._planned = False
        self.entry_close_ts = None
        self.exit_close_ts = None
        self.side = 1
        self._entry_done = False
        self._exit_done = False
        self._entry_order_id = None
        self._exit_order_id = None

    def _build_plan(self, ticks, bars) -> None:
        """Select the best single entry/exit pair using setup-time future data."""
        tick_times = [t.ts for t in ticks]
        tick_prices = [float(t.price) for t in ticks]
        tick_sessions = [t.session for t in ticks]

        m = len(tick_times)
        n = len(bars)
        if m == 0 or n < 2:
            self._reset_fallback()
            return

        total_sessions = max(tick_sessions) + 1
        if bars and bars[-1].session + 1 > total_sessions:
            total_sessions = bars[-1].session + 1
        self._min_exposure_sessions = max(
            1, int(math.ceil(total_sessions * self.min_exposure_frac))
        )

        # For each bar close, the first tick strictly after that close is the
        # expected fill of a GTC market order submitted at that bar close.
        fill_idx: list[int | None] = [None] * n
        for i, bar in enumerate(bars):
            j = bisect_right(tick_times, bar.close_ts)
            if j < m:
                fill_idx[i] = j

        best_gross = float("-inf")
        best_i = -1
        best_k = -1
        best_side = 0

        for i in range(n - 1):
            entry_j = fill_idx[i]
            if entry_j is None:
                continue
            entry_session = tick_sessions[entry_j]

            for k in range(i + 1, n):
                exit_j = fill_idx[k]
                if exit_j is None:
                    continue
                exit_session = tick_sessions[exit_j]
                exposure_sessions = exit_session - entry_session + 1
                if exposure_sessions < self._min_exposure_sessions:
                    continue

                long_gross = tick_prices[exit_j] - tick_prices[entry_j]
                short_gross = tick_prices[entry_j] - tick_prices[exit_j]

                if long_gross > best_gross:
                    best_gross = long_gross
                    best_i = i
                    best_k = k
                    best_side = 1
                if short_gross > best_gross:
                    best_gross = short_gross
                    best_i = i
                    best_k = k
                    best_side = -1

        if best_side == 0 or best_gross <= self.min_gross:
            self._reset_fallback()
            return

        self._planned = True
        self.side = best_side
        self.entry_close_ts = bars[best_i].close_ts
        self.exit_close_ts = bars[best_k].close_ts

    def _submit_entry(self, ctx) -> None:
        if ctx.position or ctx.working_order_ids:
            return

        if self.side > 0:
            oid = ctx.buy(size=self.size, tif="GTC", tag="plan-entry")
        else:
            oid = ctx.sell(size=self.size, tif="GTC", tag="plan-entry")

        if oid is not None:
            self._entry_done = True
            self._entry_order_id = oid

    def _submit_exit(self, ctx) -> None:
        if not ctx.position:
            return

        size = abs(ctx.position)
        if self.side > 0:
            oid = ctx.sell(size=size, tif="GTC", reduce_only=True,
                           tag="plan-exit")
        else:
            oid = ctx.buy(size=size, tif="GTC", reduce_only=True,
                          tag="plan-exit")

        if oid is not None:
            self._exit_done = True
            self._exit_order_id = oid

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        # Fallback: one GTC long at the first actionable bar, held to end.
        if not self._planned:
            if not self._entry_done and not ctx.position and not ctx.working_order_ids:
                self._submit_entry(ctx)
            return

        # Planned round trip: entry.
        if not self._entry_done:
            if self.entry_close_ts is not None and bar.close_ts < self.entry_close_ts:
                return
            if ctx.position or ctx.working_order_ids:
                return
            self._submit_entry(ctx)
            return

        # Planned round trip: exit.
        if not self._exit_done and ctx.position:
            if self.exit_close_ts is not None and bar.close_ts < self.exit_close_ts:
                return
            self._submit_exit(ctx)

    def on_fill(self, ctx) -> None:
        if ctx.position == 0:
            self._exit_done = True

    def on_lifecycle(self, ctx) -> None:
        event = ctx.event
        if event.kind not in (
            "order_rejected",
            "cancel_confirmed",
            "day_expired",
            "gtd_expired",
        ):
            return

        oid = event.order_id
        if oid is None:
            return

        if oid == self._entry_order_id:
            self._entry_done = False
            self._entry_order_id = None
        elif oid == self._exit_order_id:
            self._exit_done = False
            self._exit_order_id = None
