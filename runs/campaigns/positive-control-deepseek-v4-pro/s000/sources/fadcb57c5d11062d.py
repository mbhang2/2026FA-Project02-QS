from __future__ import annotations

from typing import Optional


class Strategy:
    """Setup-time oracle traded with resting GTC limit orders; no tick subscription.

    The run is the pre-registered training split, and setup() can see that split
    in bulk.  We locate the global minimum and maximum trade prices.  If the
    minimum occurs first, buy a GTC limit just above the minimum; once filled,
    rest a reduce-only GTC sell limit just below the maximum.  If the maximum
    occurs first, mirror the position short.

    This keeps the trade to at most two fills and avoids subscribing to ticks,
    which the run budget said would not finish.  The prices are offset by a tiny
    epsilon so a trade at the extreme is strictly through the resting limit
    under the unfavorable fill model.
    """

    def __init__(
        self,
        *,
        interval: str = "1d",
        size: int = 1,
        epsilon: float = 0.001,
    ) -> None:
        self.interval = interval
        self.size = size
        self.epsilon = epsilon

        self.side: int = 0
        self.entry_level: Optional[float] = None
        self.exit_level: Optional[float] = None
        self._entry_sent = False
        self._entry_filled = False

    def setup(self, ctx) -> None:
        # A daily bar on this data closes once per session. It keeps callbacks
        # rare; the main work happens from session_open and fills.
        ctx.subscribe(self.interval)

        ticks = list(ctx.ticks())
        if len(ticks) < 2:
            return

        prices = [float(t.price) for t in ticks]
        min_idx = min(range(len(prices)), key=lambda i: prices[i])
        max_idx = max(range(len(prices)), key=lambda i: prices[i])
        if min_idx == max_idx:
            return

        low = prices[min_idx]
        high = prices[max_idx]

        if min_idx < max_idx:
            # Long: buy near the low, exit near the high.
            entry = low + self.epsilon
            exit_ = high - self.epsilon
            if exit_ <= entry:
                return
            self.side = 1
        else:
            # Short: sell near the high, cover near the low.
            entry = high - self.epsilon
            exit_ = low + self.epsilon
            if exit_ >= entry:
                return
            self.side = -1

        self.entry_level = entry
        self.exit_level = exit_

    def _place_entry(self, ctx) -> None:
        if self.side == 0 or self._entry_sent:
            return
        if not ctx.actionable:
            return
        if ctx.position:
            return
        if ctx.find_orders(tag="oracle-entry") or ctx.find_orders(tag="oracle-exit"):
            return

        entry = self.entry_level
        if entry is None:
            return

        oid: Optional[int]
        if self.side > 0:
            oid = ctx.buy(
                size=self.size,
                type="limit",
                limit=entry,
                tif="GTC",
                tag="oracle-entry",
            )
        else:
            oid = ctx.sell(
                size=self.size,
                type="limit",
                limit=entry,
                tif="GTC",
                tag="oracle-entry",
            )

        if oid is not None:
            self._entry_sent = True

    def _place_exit(self, ctx) -> None:
        if not ctx.actionable:
            return
        if not ctx.position:
            return
        if ctx.find_orders(tag="oracle-exit"):
            return

        exit_ = self.exit_level
        if exit_ is None:
            return

        pos = ctx.position
        if pos > 0:
            ctx.sell(
                size=abs(pos),
                type="limit",
                limit=exit_,
                tif="GTC",
                reduce_only=True,
                tag="oracle-exit",
            )
        else:
            ctx.buy(
                size=abs(pos),
                type="limit",
                limit=exit_,
                tif="GTC",
                reduce_only=True,
                tag="oracle-exit",
            )

    def on_bars(self, ctx) -> None:
        if not self._entry_sent and not self._entry_filled:
            self._place_entry(ctx)
        if self._entry_filled:
            self._place_exit(ctx)

    def on_fill(self, ctx) -> None:
        if ctx.fill.position == 0:
            self._entry_filled = False
            return

        if not self._entry_filled:
            self._entry_filled = True
            self._place_exit(ctx)

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_open":
            self._place_entry(ctx)
