"""Use the training split in `setup` to choose a direction and a patient limit
entry, then hold the position to the end of the run.

Prior trials show the least-bad result is one entry, no exit, maximum exposure.
This keeps that shape but lets `setup` read the aggregated training bars once and
pick the side (long or short) and a pullback limit distance that would have
produced the best gross P&L from the first closed bar to the last closed bar,
under a simple fallback: if the limit has not filled after a fixed number of
bars, cancel it and enter at market.

No future bar is read in `on_bars`; everything used there is precomputed in
`setup` from the training split, which the framework makes available for exactly
this purpose.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        max_wait_bars: int = 24,
    ):
        self.interval = interval
        self.size = size
        self.max_wait_bars = max_wait_bars

        # Fallback defaults, also used if setup does not produce enough data.
        self.side = 1          # 1 = long, -1 = short
        self.entry_offset = 0.0

        self._started = False
        self._bars_waiting = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

        try:
            bars = ctx.train_bars(self.interval)
            if bars and len(bars) >= 2:
                self._fit_parameters(bars)
        except Exception:
            # Never let a setup-time analysis crash the trial.  The strategy
            # falls back to the conservative long-at-market hold.
            self.side = 1
            self.entry_offset = 0.0

    def _fit_parameters(self, bars) -> None:
        """Choose side and a limit offset from the training bar path."""
        first_close = bars[0].close
        last_close = bars[-1].close

        # Use a typical bar range as the scale for candidate offsets so the
        # search adapts to whatever price level the simulated instrument uses.
        ranges = [b.high - b.low for b in bars if b.high > b.low]
        if ranges:
            ranges.sort()
            unit = ranges[len(ranges) // 2]
        else:
            unit = max(abs(first_close) * 0.005, 0.001)

        multipliers = (0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0)
        candidates: list[float] = []
        for mult in multipliers:
            offset = unit * mult
            if not candidates or abs(offset - candidates[-1]) > 1e-9:
                candidates.append(offset)

        best_score = None
        best_side = 1
        best_offset = 0.0

        for side in (1, -1):
            for offset in candidates:
                if offset == 0.0:
                    entry_price = first_close
                else:
                    entry_price = self._simulate_limit(
                        bars,
                        side=side,
                        offset=offset,
                        first_close=first_close,
                        last_close=last_close,
                    )
                    if entry_price is None:
                        continue

                if side == 1:
                    score = last_close - entry_price
                else:
                    score = entry_price - last_close

                if best_score is None or score > best_score:
                    best_score = score
                    best_side = side
                    best_offset = offset

        self.side = best_side
        self.entry_offset = best_offset

    def _simulate_limit(self, bars, *, side, offset, first_close, last_close):
        """Return an approximate fill price for a limit entry, or None.

        A limit order is considered filled if a later bar trades strictly
        through it.  If it never fills within `max_wait_bars`, assume a market
        fallback at the close of that bar.
        """
        if len(bars) < self.max_wait_bars + 2:
            return None

        if side == 1:
            limit = first_close - offset
        else:
            limit = first_close + offset

        # Scan bars after the first bar close; the limit is eligible only after
        # that close (T+1).
        for i in range(1, self.max_wait_bars + 1):
            b = bars[i]
            if side == 1:
                if b.low < limit:
                    return limit
            else:
                if b.high > limit:
                    return limit

        idx = self.max_wait_bars
        if idx >= len(bars):
            return None
        return bars[idx].close

    def _place_market(self, ctx) -> None:
        if self.side == 1:
            ctx.buy(size=self.size, tif="GTC", tag="entry-market")
        else:
            ctx.sell(size=self.size, tif="GTC", tag="entry-market")

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Hold to the end; one position at a time.
        if ctx.position:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        last = history[-1]

        working_limit = ctx.find_orders(tag="entry-limit")
        if working_limit:
            self._bars_waiting += 1
            if self._bars_waiting >= self.max_wait_bars:
                ctx.cancel(working_limit[0].id)
                self._place_market(ctx)
            return

        # A market entry may already be working; wait for it.
        if ctx.working_order_ids:
            return

        if not self._started:
            self._started = True
            self._bars_waiting = 0

            if self.entry_offset == 0.0:
                self._place_market(ctx)
                return

            if self.side == 1:
                limit_price = last.close - self.entry_offset
                ctx.buy(
                    size=self.size,
                    type="limit",
                    limit=limit_price,
                    tif="GTC",
                    tag="entry-limit",
                )
            else:
                limit_price = last.close + self.entry_offset
                ctx.sell(
                    size=self.size,
                    type="limit",
                    limit=limit_price,
                    tif="GTC",
                    tag="entry-limit",
                )
            return

        # Started, no position, no working order: retry a market entry.
        self._place_market(ctx)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
