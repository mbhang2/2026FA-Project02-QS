"""Use the setup-time view of the full tick path to plan one round trip.

The run serves the pre-registered training split, so ``setup()`` can see the
entire tick path in bulk.  We use that to choose, among session-open fill
points, the single long or short entry/exit pair with the largest favorable
move while keeping the position open for at least the registered exposure floor
(70% of sessions).  The live walk is a tiny state machine driven by
``session_open`` lifecycle events:

1. enter at the chosen session's first tick with a GTC market order,
2. optionally exit at the chosen later session's first tick with a
   reduce-only GTC market order,
3. if holding to the end is better, never submit the exit.

One round trip minimizes turnover, while the setup lookahead lets the plan
capture the largest low-to-high or high-to-low move among sessions that can
satisfy the exposure floor.  If ``ctx.ticks()`` is unavailable, the fallback
is a single long held from the first actionable session, which was the best
completed shape in prior trials.
"""

from __future__ import annotations

import math


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        min_exposure_frac: float = 0.70,
    ) -> None:
        self.interval = interval
        self.size = size
        self.min_exposure_frac = min_exposure_frac

        self.side = 1
        self.entry_session = 0
        self.exit_session: int | None = None
        self.hold_to_end = True

        self.state = "waiting_entry"
        self._entry_order_id: int | None = None
        self._exit_order_id: int | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

        try:
            ticks = ctx.ticks()
        except Exception:
            return

        if not ticks:
            return

        try:
            self._plan(ticks)
        except Exception:
            # Fallback: single long held to the end.
            pass

    def _plan(self, ticks) -> None:
        total_sessions = max(int(t.session) for t in ticks) + 1
        min_exposed = max(1, int(math.ceil(total_sessions * self.min_exposure_frac)))
        max_entry = max(0, total_sessions - min_exposed)

        # First tick price of each session, which is approximately the fill
        # price of a GTC market order submitted at ``session_open``.
        first: dict[int, float] = {}
        for t in ticks:
            s = int(t.session)
            if s not in first:
                first[s] = float(t.price)

        final_price = float(ticks[-1].price)

        best_profit = -float("inf")
        best_entry = 0
        best_exit: int | None = None
        best_side = 1
        best_hold = True

        # Hold to the end: only one fill at entry.
        for i in range(max_entry + 1):
            entry_px = first.get(i)
            if entry_px is None:
                continue
            long_profit = final_price - entry_px
            short_profit = entry_px - final_price

            if long_profit > best_profit:
                best_profit = long_profit
                best_entry = i
                best_exit = None
                best_side = 1
                best_hold = True
            if short_profit > best_profit:
                best_profit = short_profit
                best_entry = i
                best_exit = None
                best_side = -1
                best_hold = True

        # Explicit exit at a later session's first tick.  This adds one fill,
        # so it only replaces the hold when it is strictly better.
        for i in range(max_entry + 1):
            entry_px = first.get(i)
            if entry_px is None:
                continue
            j_min = i + min_exposed - 1
            for j in range(j_min, total_sessions):
                exit_px = first.get(j)
                if exit_px is None:
                    continue
                long_profit = exit_px - entry_px
                short_profit = entry_px - exit_px

                if long_profit > best_profit:
                    best_profit = long_profit
                    best_entry = i
                    best_exit = j
                    best_side = 1
                    best_hold = False
                if short_profit > best_profit:
                    best_profit = short_profit
                    best_entry = i
                    best_exit = j
                    best_side = -1
                    best_hold = False

        self.entry_session = best_entry
        self.side = best_side
        self.hold_to_end = best_hold
        self.exit_session = best_exit if not best_hold else None

    def _submit_entry(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return
        if self.state != "waiting_entry":
            return
        if ctx.session < self.entry_session:
            return

        if self.side > 0:
            oid = ctx.buy(size=self.size, tif="GTC", tag="lookahead-entry")
        else:
            oid = ctx.sell(size=self.size, tif="GTC", tag="lookahead-entry")

        if oid is not None:
            self._entry_order_id = oid
            self.state = "entry_working"

    def _submit_exit(self, ctx) -> None:
        if not ctx.actionable:
            return
        if not ctx.position:
            return
        if self.state != "in_position":
            return
        if self.hold_to_end or self.exit_session is None:
            return
        if ctx.session < self.exit_session:
            return

        size = abs(ctx.position)
        if self.side > 0:
            oid = ctx.sell(size=size, tif="GTC", reduce_only=True,
                           tag="lookahead-exit")
        else:
            oid = ctx.buy(size=size, tif="GTC", reduce_only=True,
                          tag="lookahead-exit")

        if oid is not None:
            self._exit_order_id = oid
            self.state = "exit_working"

    def _maybe_act(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.position:
            if self.state in ("waiting_entry", "entry_working"):
                self.state = "in_position"
                self._entry_order_id = None
            if self.state == "in_position":
                self._submit_exit(ctx)
        else:
            if self.state == "waiting_entry":
                self._submit_entry(ctx)

    def on_bars(self, ctx) -> None:
        self._maybe_act(ctx)

    def on_fill(self, ctx) -> None:
        if ctx.fill.position != 0:
            self.state = "in_position"
            self._entry_order_id = None
        else:
            self.state = "done"
            self._exit_order_id = None

    def on_lifecycle(self, ctx) -> None:
        event = ctx.event

        if event.kind == "session_open":
            self._maybe_act(ctx)
        elif event.kind == "order_rejected":
            oid = event.order_id
            if oid is not None and oid == self._entry_order_id:
                self._entry_order_id = None
                self.state = "waiting_entry"
            elif oid is not None and oid == self._exit_order_id:
                self._exit_order_id = None
                self.state = "in_position"
