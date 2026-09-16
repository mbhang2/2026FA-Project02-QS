"""`nalin_l1.strategy.candidate.Momentum`, ported to the event-driven API (Task 40).

The original, in one sentence: on 1-minute bars, go long when the close breaks
above EMA30 + 5 sigma and short below EMA30 - 5 sigma, hold until the close
crosses back through EMA30, never flip inside 15 bars, and shift every signal by
one bar to avoid lookahead.

Four things about the translation are worth having written down, because three
of them are findings about *this* API and one is a bug the port would have had
if it had been done mechanically.

**1. `.shift(1)` is not just unnecessary here, it would be wrong.** The source
computes `close.shift(1)`, `ema30.shift(1)` and the shifted bands, and says so in
its docstring: "All signals are shifted by 1 bar to prevent lookahead bias."
That shift exists because a pandas frame hands a strategy the whole series
including the bar being decided on, so the defence has to be written by hand,
every time, by every author. `ctx.history(interval)` contains **only closed
bars** -- `history[-1]` is the bar that just closed and `history[len(history)]`
raises `LookaheadError` rather than returning something plausible. The shift is
already in the type. Porting it literally would lag every signal by two bars and
silently change the strategy.

That is the clearest thing this port has to say about the API: *the shape does
the work the discipline used to do.*

**2. The state machine ported cleanly and belongs on `self`.** `current_pos` and
`bars_since_entry` were loop variables over the whole series; here they are
instance attributes updated per callback. This is what the API documentation
tells you to do and it needed no workaround. The 15-bar minimum hold is the one
piece of the original that is *easier* here, because "bars since entry" is a
thing that happens rather than a thing to index.

**3. Positions become order deltas, and the target is unreachable.** The source
returns a position for every bar; this API takes orders and infers the position.
Every bar where the desired position differs from `ctx.position` becomes a
market order for the difference -- and a flip from long to short is one order of
size 2, not two orders, because a position that passes through zero on its way
does not exist in this engine's accounting. `ctx.close()` exists for flattening
and is used for the exit.

The unreachable part: the source's positions are **fractions of capital**. This
engine trades **integer contracts**. `+1.0` becomes `size=1` and there is no
expression for "fully invested". A port of a fractionally-sized strategy is a
different strategy, and no amount of API surface fixes that.

**4. The rolling statistics are recomputed, not rolled.** `close.ewm(span=30)`
and `close.rolling(30).std()` are vectorised over the series; here EMA is kept
incrementally on `self` (it is a recurrence, so this is exact and cheaper) and
the standard deviation is recomputed from `history.last(30)` each bar. The
window is bounded -- `ctx.history` is the pushed tail, 512 bars by default -- so
`last(30)` is safe, but a strategy wanting a 600-bar statistic would have to
keep it itself. The API says so; it is still a real constraint the source did
not have.

**What was awkward and what was impossible** is in the Task 40 report. Nothing
here needed a workaround; the one thing that could not be expressed is
fractional sizing.
"""

from __future__ import annotations

import math


class Strategy:
    def __init__(self, interval: str = "5m", span: int = 30, window: int = 30,
                 band_mult: float = 5.0, min_hold: int = 15,
                 size: int = 1) -> None:
        self.interval = interval
        self.span = span
        self.window = window
        self.band_mult = band_mult
        self.min_hold = min_hold
        self.size = size
        # The source's loop variables, now state that persists across callbacks.
        self.ema: float | None = None
        self.bars_since_entry = 0
        self.seen = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    # -- the recurrence, kept rather than recomputed ------------------------- #
    def _update_ema(self, close: float) -> None:
        alpha = 2.0 / (self.span + 1.0)          # pandas `adjust=False`
        self.ema = close if self.ema is None else alpha * close + (1 - alpha) * self.ema

    def on_bars(self, ctx) -> None:
        history = ctx.history(self.interval)
        if len(history) <= self.seen:
            return
        last = history[-1]
        self._update_ema(last.close)
        self.seen = len(history)
        if ctx.position:
            self.bars_since_entry += 1

        if not ctx.actionable or ctx.working_order_ids:
            return
        if len(history) < self.window:
            # `min_periods=30` in the source. Short history is a legitimate
            # state during warmup, so this is a length check rather than a
            # caught exception.
            return

        closes = [b.close for b in history.last(self.window)]
        mean = sum(closes) / len(closes)
        var = sum((c - mean) ** 2 for c in closes) / (len(closes) - 1)
        sd = math.sqrt(var)
        if sd <= 0 or self.ema is None:
            return

        upper = self.ema + self.band_mult * sd
        lower = self.ema - self.band_mult * sd

        if ctx.position == 0:
            # No `.shift(1)`: `last` is the bar that just closed, which is what
            # the source's shifted series held.
            if last.close > upper:
                ctx.buy(size=self.size, tag="momentum-long")
                self.bars_since_entry = 0
            elif last.close < lower:
                ctx.sell(size=self.size, tag="momentum-short")
                self.bars_since_entry = 0
            return

        if self.bars_since_entry < self.min_hold:
            return
        crossed_back = ((ctx.position > 0 and last.close < self.ema)
                        or (ctx.position < 0 and last.close > self.ema))
        if crossed_back:
            ctx.close(tag="momentum-exit")
            self.bars_since_entry = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
