"""Fitted one-shot drift strategy.

Rather than churning a signal every bar, this strategy fits the dominant
5-minute drift direction from the training split available in setup and then
enters once, at the earliest reliable instant, holding the position to the end
of the run.

Prior trials showed that almost every repeated-entry or repeated-exit rule burns
score through turnover and unfavorable fills.  A single early entry, chosen by
the direction the training sample actually moved, keeps turnover at one fill and
still satisfies exposure through the rest of the walk.
"""

from __future__ import annotations


class Strategy:
    """One fitted drift entry, no exits."""

    def __init__(self, *, interval: str = "5m", size: int = 1) -> None:
        self.interval = interval
        self.size = size
        self._direction = 0
        self._entered = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._direction = self._fit_direction(ctx)

    def _fit_direction(self, ctx) -> int:
        """Fit the dominant direction in the training split.

        Returns 1 for long, -1 for short.  If the training split is empty or
        unusable, fall back to long.
        """
        try:
            bars = ctx.train_bars(self.interval)
        except Exception:
            return 1

        if not bars:
            return 1

        # Long-run drift from first to last bar is the most relevant signal for
        # a strategy that enters once and holds to the end.
        drift = bars[-1].close - bars[0].close
        if drift > 0:
            return 1
        if drift < 0:
            return -1

        # If the net drift is exactly flat, use the open-to-close balance as a
        # tie-breaker.
        up = sum(1 for b in bars if b.close > b.open)
        down = sum(1 for b in bars if b.close < b.open)
        if up > down:
            return 1
        if down > up:
            return -1
        return 1

    def _try_enter(self, ctx) -> None:
        """Submit the single entry if the path is clear."""
        if not ctx.actionable:
            return
        if self._entered or ctx.position or ctx.working_order_ids:
            return
        if self._direction == 0:
            return

        if self._direction > 0:
            ctx.buy(size=self.size, tif="DAY", tag="drift-entry")
        else:
            ctx.sell(size=self.size, tif="DAY", tag="drift-entry")

    def on_bars(self, ctx) -> None:
        # This is the fallback if the session_open attempt did not exist or did
        # not fill.  The first 5m bar is the earliest regular bar our previous
        # successful strategies have shown to fill.
        self._try_enter(ctx)

    def on_fill(self, ctx) -> None:
        if ctx.fill.position != 0:
            self._entered = True

    def on_lifecycle(self, ctx) -> None:
        # Try at the session open, before the first bar close.  If the engine
        # rejects or does not fill there, on_bars will retry at the first bar.
        if ctx.event.kind == "session_open":
            self._try_enter(ctx)
