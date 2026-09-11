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

**The session end is read, not guessed.** `ctx.session_end` is the exchange's
published close for the session in progress, so the exit rule is one comparison:
flatten when less than `exit_margin_ns` of the session remains. Everything else
is gone.

Two guesses preceded it, and both were wrong in ways only real data showed. The
first counted bars between session opens -- but empty intervals are skipped, so a
bar count measures *activity*, not length: over 384 NQ training sessions the
running minimum collapsed from 276 bars to 102 because one quiet holiday session
produced few non-empty bars, and the strategy then stopped trading two thirds of
the way through every subsequent session for the rest of the run. The second
learned the shortest *duration* seen, which is a real property of a session and
still cannot help: every full CME session is 23 hours, so a shortened one never
reaches the trigger, and 12 of 384 training sessions ended with a position open.
Every one of them was an early close.

No amount of learning fixes this, because the information is not in the past. A
half day arrives without warning and is exactly when being caught long overnight
matters.
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
        # How much of the session to leave for the exit to fill in. Ten minutes
        # is two 5m bars; it needs to be long enough that a market order queued
        # on a bar close still has ticks to fill against.
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
        self._prev_close: float | None = None
        self.sessions_left_open = 0   # should stay 0; the rig asserts it

    def setup(self, ctx) -> None:
        # `emit_empty` so the exit rule cannot be starved. Reading
        # `ctx.session_end` tells the strategy when the session closes, but a
        # callback is still needed to act on it, and a quiet tail to a session
        # produces no bars at all -- so a position survived the boundary anyway,
        # once in 120 sessions. An empty bar is the heartbeat: every interval
        # arrives, whether or not anything traded in it.
        ctx.subscribe(self.interval, emit_empty=True)
        self._ranges.clear()
        self._session = None
        self._prev_close = None
        self.sessions_left_open = 0

    def on_bars(self, ctx) -> None:
        history = ctx.history(self.interval)
        if not len(history):
            return
        last = history[-1]
        if last.tick_count:
            # True range, not the bar's own high-low: a bar that opened away from
            # the previous close, or a thin one whose range understates what the
            # price actually did, would otherwise shrink the volatility estimate
            # and tighten the bracket exactly when it should widen.
            #
            # Empty bars contribute nothing. They carry the previous close
            # forward, so their range is zero and counting them would drag the
            # estimate toward zero in precisely the quiet stretches that precede
            # a volatile one.
            prev_close = self._prev_close
            self._prev_close = last.close
            if prev_close is None:
                self._ranges.append(last.high - last.low)
            else:
                self._ranges.append(
                    max(last.high, prev_close) - min(last.low, prev_close)
                )

        if ctx.session != self._session:
            self._session = ctx.session
            # Nothing should ever survive a boundary now. Counted rather than
            # assumed: the rig asserts this stays zero, and if it does not the
            # exit margin is too short for the session's tick density.
            if ctx.position or ctx.working_order_ids:
                self.sessions_left_open += 1
                if ctx.working_order_ids:
                    ctx.cancel_all()
                if ctx.position:
                    ctx.close()
                return

        late = ctx.session_end - ctx.ts <= self.exit_margin_ns
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
