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

The session length is learned rather than assumed, and **learned as a duration
rather than as a bar count**. Counting bars was wrong in a way only real data
showed. Empty intervals are skipped unless `emit_empty` is set, so a bar count is
a measure of *activity*, not of length: over the 384 NQ training sessions the
running minimum collapsed from 276 bars to 102, because one quiet holiday session
produced few non-empty bars. The strategy then stopped trading roughly two thirds
of the way through every subsequent session, for the rest of the run, and on the
synthetic generator -- where every interval has ticks and every session is the
same length -- nothing could have shown it.

A duration is a property of the session. It does not move with how busy the
session was.

**What this still cannot do.** A strategy cannot know that *this* session ends
early until it has ended, and the engine does not tell it when the session
closes. So the first session shorter than anything seen before can still strand a
position, and one did: 2025-11-28, the half day after Thanksgiving. The
tabulation excludes the sessions affected by a carry rather than pretending the
apparatus is flat when it is not -- see `null.py`. Closing this properly needs the
context to expose the session's end, which is an engine change and a separate
task.
"""

from __future__ import annotations

from collections import deque

from ...types import NS_PER_SECOND


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
        # A duration, not a bar count: the point at which the strategy stops
        # opening and flattens, measured back from the shortest session it has
        # seen. Ten minutes is two 5m bars, which is what the bar-count version
        # meant on a dataset where every interval had ticks.
        exit_margin_ns: int = 10 * 60 * NS_PER_SECOND,
    ):
        self.interval = interval
        self.atr_window = atr_window
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.size = size
        self.min_bracket = min_bracket
        self.exit_margin_ns = exit_margin_ns
        self._ranges: deque[float] = deque(maxlen=atr_window)
        self._session: int | None = None
        self._session_open_ts: int | None = None
        self._last_ts: int | None = None
        self._min_duration_ns: int | None = None  # shortest session seen, learned
        self.sessions_left_open = 0               # residual, reported by the rig

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self._ranges.clear()
        self._session = None
        self._session_open_ts = None
        self._last_ts = None
        self._min_duration_ns = None
        self.sessions_left_open = 0

    def on_bars(self, ctx) -> None:
        history = ctx.history(self.interval)
        if not len(history):
            return
        last = history[-1]
        self._ranges.append(last.high - last.low)

        if ctx.session != self._session:
            if self._session is not None and self._session_open_ts is not None:
                duration = self._last_ts - self._session_open_ts
                if duration > 0:
                    self._min_duration_ns = (
                        duration
                        if self._min_duration_ns is None
                        else min(self._min_duration_ns, duration)
                    )
            self._session = ctx.session
            self._session_open_ts = last.open_ts
            # A position that survived the boundary is a session shorter than
            # anything seen before. Flatten it at the first opportunity in the
            # new session so the contamination stops at one carry, and count it
            # so the tabulation can exclude what it touched.
            if ctx.position or ctx.working_order_ids:
                self.sessions_left_open += 1
                if ctx.working_order_ids:
                    ctx.cancel_all()
                if ctx.position:
                    ctx.close()
                self._last_ts = ctx.ts
                return
        self._last_ts = ctx.ts

        late = (
            self._min_duration_ns is not None
            and self._session_open_ts is not None
            and (ctx.ts - self._session_open_ts)
            >= self._min_duration_ns - self.exit_margin_ns
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
        if self._min_duration_ns is None:
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
