"""The strategy the calibration rig measures with. Immutable apparatus.

It used to be `src/strategies/current.py` — the agent's file — which meant the
power curve measured the metric, the threshold and whatever that file contained,
with no way to tell them apart. Task 4's sweep turned over at the strongest
planted signal and it took a separate diagnostic to establish that the cause was
the strategy rather than the metric. That diagnostic should not have been needed.

**The bracket scales with realized volatility.** That is the whole point of this
file. With a fixed bracket every win is capped at the same size while a stop
gapped through under the pessimistic fill model loses more the stronger the trend
— mean win pinned at 0.740 while mean loss grew from 1.54 to 31.45 across the
sweep. The payoff shape changed with signal strength, so "acceptance must be
monotone in signal strength" was never a property of the metric alone. Sizing the
bracket in units of recent bar range holds the shape constant by construction and
makes monotonicity testable again.

**It is also flat at every session boundary**, and that is apparatus design
rather than trading advice. The synthetic generator starts every session at the
same price, so a position carried across the gap realizes the whole of the
previous session's drift as a loss — measured at signal 1.0, same-session trades
averaged +1.30 and the twenty-two overnight ones averaged −16.78, and every one
of the five worst losses in the run was an overnight hold. Carrying overnight
measures the generator's discontinuity, not the metric. A dataset whose price
process is continuous across sessions would not need this.

The session length is learned rather than assumed: the strategy counts bars
between session opens, so it works on any dataset without being told.
"""

from __future__ import annotations

from collections import deque


class ReferenceStrategy:
    """Follow the last closed bar, with a volatility-scaled bracket.

    `atr_window` bars of average range set the unit; `stop_atr` and `target_atr`
    are multiples of it. Nothing here is meant to be good — it is meant to have a
    payoff whose *shape* does not move when the data's scale does.
    """

    name = "reference/vol-scaled"

    def __init__(
        self,
        *,
        interval: str = "5m",
        atr_window: int = 20,
        stop_atr: float = 1.5,
        target_atr: float = 1.5,
        size: int = 1,
        min_bracket: float = 0.25,
        exit_margin_bars: int = 2,
    ):
        self.interval = interval
        self.atr_window = atr_window
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.size = size
        self.min_bracket = min_bracket
        self.exit_margin_bars = exit_margin_bars
        self._ranges: deque[float] = deque(maxlen=atr_window)
        self._session: int | None = None
        self._bars_this_session = 0
        self._session_bars: int | None = None   # learned from the first full session

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._ranges.clear()
        self._session = None
        self._bars_this_session = 0
        self._session_bars = None

    def on_bars(self, ctx) -> None:
        history = ctx.history(self.interval)
        if not len(history):
            return
        last = history[-1]
        self._ranges.append(last.high - last.low)

        if ctx.session != self._session:
            if self._session is not None and self._bars_this_session:
                # the shortest session seen; a half day must not strand a position
                self._session_bars = (
                    self._bars_this_session
                    if self._session_bars is None
                    else min(self._session_bars, self._bars_this_session)
                )
            self._session = ctx.session
            self._bars_this_session = 0
        self._bars_this_session += 1

        late = (
            self._session_bars is not None
            and self._bars_this_session >= self._session_bars - self.exit_margin_bars
        )
        if late:
            # Flat before the boundary. Cancel first: a bracket leg left working
            # after the position closes would open a new one.
            if ctx.working_order_ids:
                ctx.cancel_all()
            if ctx.position:
                ctx.close()
            return

        if ctx.position or ctx.working_order_ids:
            return  # one position at a time; the bracket does the exits
        if self._session_bars is None:
            return  # session length not learned yet; warmup covers this
        if len(self._ranges) < self.atr_window:
            return  # the volatility estimate is not ready
        if last.close == last.open:
            return

        atr = sum(self._ranges) / len(self._ranges)
        if atr <= 0:
            return
        stop = max(self.min_bracket, self.stop_atr * atr)
        target = max(self.min_bracket, self.target_atr * atr)

        place = ctx.buy if last.close > last.open else ctx.sell
        place(size=self.size, stop_offset=stop, target_offset=target, tag="entry")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
