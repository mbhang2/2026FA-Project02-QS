"""Does an outsized move inside a session continue, or give it back?

First proposal in the campaign. Measures "large" as the close-to-close move of
the just-closed 5m bar relative to the session's realized range so far, using
the 90th percentile of that ratio across the session's own bars as the
threshold. Trades continuation: if the move is unusually large in a direction,
enter in that direction with a bracket, expecting the flow to continue.

The threshold is fitted on the training split in setup(), using the same
per-session percentile logic, so it is a fitted strategy. The campaign is not
registered as fitted, so this is an ordinary strategy with a hardcoded
threshold percentile (90) rather than a fitted one; the threshold itself is
computed live from each session's own history, which is not fitting.

The signal:
  - On each 5m bar close, if flat and no working orders:
    - Compute the session's bars so far (from ctx.history).
    - Compute the ratio of the last bar's |close - open| to the session's
      realized range so far (max high - min low).
    - If the ratio exceeds the 90th percentile of ratios seen so far in this
      session, and the bar's direction is clear, enter in that direction.
  - Bracket with stop and target offsets set to a fraction of the session's
    realized range so far, so risk scales with volatility.

The design assumes continuation: an outsized move is information, not
exhaustion. The bracket protects against the give-back hypothesis.
"""

from __future__ import annotations


class Strategy:
    """Continuation on outsized intra-session moves."""

    def __init__(self, *, interval: str = "5m", size: int = 1,
                 percentile: float = 0.90, stop_mult: float = 1.0,
                 target_mult: float = 1.5):
        self.interval = interval
        self.size = size
        self.percentile = percentile
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self._session = None
        self._session_ratios: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_ratios = []

        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        # Only consider bars from the current session.
        session_bars = [b for b in history if b.session == ctx.session]
        if len(session_bars) < 2:
            return

        last = session_bars[-1]
        if last.close == last.open:
            return

        # Session realized range so far, excluding the current bar.
        prior = session_bars[:-1]
        high = max(b.high for b in prior)
        low = min(b.low for b in prior)
        rng = high - low
        if rng <= 0:
            return

        # Ratio of this bar's move to the session range so far.
        move = abs(last.close - last.open)
        ratio = move / rng

        # Update the session's ratio history with this bar's ratio.
        self._session_ratios.append(ratio)

        # Need enough ratios to estimate a percentile.
        if len(self._session_ratios) < 5:
            return

        # Compute the threshold as the percentile of ratios seen so far.
        sorted_ratios = sorted(self._session_ratios)
        idx = int(self.percentile * (len(sorted_ratios) - 1))
        threshold = sorted_ratios[idx]

        if ratio < threshold:
            return

        # Outsized move: trade continuation.
        direction = 1 if last.close > last.open else -1
        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if direction > 0:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="outsized-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="outsized-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
