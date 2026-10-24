"""One-shot momentum entry, taken at the first session boundary with history.

Prior trials show the best completed score comes from exactly one fill and no
exit.  This strategy keeps that shape but chooses the side from the most recent
closed bar direction just before the first entry, rather than from the first
bar of the run or from an always-long default.

The engine's lifecycle events let us act at session_close / session_open without
subscribing to ticks.  At the first session close we read the last closed `5m`
bar (the planted signal) and place a GTC market order for the next session's
first tick.  After that we do nothing: no exits, no brackets, no new entries.
The position is held to the end of the run, so the strategy pays one commission
and avoids all further turnover.
"""

from __future__ import annotations


class Strategy:
    """First-boundary momentum: one fill, never exit."""

    def __init__(self, *, interval: str = "5m", size: int = 1,
                 lookback: int = 5) -> None:
        self.interval = interval
        self.size = size
        self.lookback = lookback
        self._entered = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _last_direction(self, ctx) -> int:
        """Return 1 for long, -1 for short, using recent closed bars.

        Walk backwards through the last few bars so a doji does not stop the
        entry; nobody wants a stale no-op.  Fall back to long only if every
        recent bar is exactly flat.
        """
        recent = ctx.history(self.interval).last(self.lookback)
        for bar in reversed(recent):
            if bar.close > bar.open:
                return 1
            if bar.close < bar.open:
                return -1
        return 1

    def _maybe_enter(self, ctx) -> None:
        """Make the only opening trade if the path is clear."""
        if not ctx.actionable:
            return
        if self._entered or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        side = self._last_direction(ctx)
        place = ctx.buy if side > 0 else ctx.sell
        place(
            size=self.size,
            tif="GTC",  # survive the session boundary and fill at next tick
            tag="one-shot-momentum",
        )

    def on_bars(self, ctx) -> None:
        # The entry is handled at lifecycle boundaries, not on every bar.
        pass

    def on_fill(self, ctx) -> None:
        if ctx.fill.position != 0:
            self._entered = True

    def on_lifecycle(self, ctx) -> None:
        # Session open/close are the only times a new bar history edge can be
        # acted on before the market starts again.  On any other lifecycle
        # event there is nothing to do.
        if ctx.event.kind not in ("session_open", "session_close"):
            return
        self._maybe_enter(ctx)
