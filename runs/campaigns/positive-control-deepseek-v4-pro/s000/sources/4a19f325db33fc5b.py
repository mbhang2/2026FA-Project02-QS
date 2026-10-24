"""Breakout entries with passive limit orders instead of market orders.

The prior two campaigns took every up/down bar as a momentum signal and paid
the spread/adverse-fill cost on each entry, finishing negative.  This strategy
keeps a momentum view but demands confirmation: price must close beyond the
previous N-bar high or low.  Instead of chasing that close with a market order,
we rest a limit at the breakout bar's close.  A fill therefore only happens if
price trades back through our level, giving a better average entry than a
market order and skipping many false breakouts.

The bracket is sized from the recent range and given as offsets from the fill,
so it cannot invert with slippage.  Entries and their brackets are GTC so a
position that survives a session close remains protected.  Unfilled entry
orders are cancelled at the session close so no stale orders carry over.
"""

from __future__ import annotations

ENTRY_TAG = "breakout-entry"


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 6,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        patience: int = 4,
        max_entry_fraction: float = 0.90,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.patience = patience
        self.max_entry_fraction = max_entry_fraction
        self.size = size
        self._pending_age = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        if not ctx.actionable:
            return

        session_total = ctx.session_end - ctx.session_start
        progress = (
            0.0
            if session_total <= 0
            else (ctx.ts - ctx.session_start) / session_total
        )

        # Let the bracket manage an open position.  New entries wait until flat.
        if ctx.position:
            return

        # Manage a resting breakout entry by tag, not by a stored id.
        pending = ctx.find_orders(tag=ENTRY_TAG)
        if pending:
            self._pending_age += 1
            if self._pending_age >= self.patience:
                for order in pending:
                    ctx.cancel(order.id)
                self._pending_age = 0
            return

        # Avoid opening a brand-new position in the final stretch of a session.
        if progress >= self.max_entry_fraction:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        window = history.last(self.lookback + 1)
        if len(window) < self.lookback + 1:
            return

        last = window[-1]
        prior = window[:-1]

        high = max(b.high for b in prior)
        low = min(b.low for b in prior)
        rng = high - low
        if rng <= 0:
            return

        stop_offset = rng * self.stop_mult
        target_offset = rng * self.target_mult

        if last.close > high:
            ctx.buy(
                size=self.size,
                type="limit",
                limit=last.close,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tif="GTC",
                tag=ENTRY_TAG,
            )
            self._pending_age = 0
        elif last.close < low:
            ctx.sell(
                size=self.size,
                type="limit",
                limit=last.close,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tif="GTC",
                tag=ENTRY_TAG,
            )
            self._pending_age = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        # Cancel unfilled GTC entry orders at the session close so they cannot
        # fill against stale conditions in the next session.
        if ctx.event.kind == "session_close":
            for order in ctx.find_orders(tag=ENTRY_TAG):
                ctx.cancel(order.id)
            self._pending_age = 0
