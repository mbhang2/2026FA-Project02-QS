"""Circular rotation of each session's price path against the clock.

**The null hypothesis, stated before the design.** *This candidate, with its
trading behaviour unchanged, has no edge.* A valid randomization must therefore
preserve the candidate's behavioural profile -- trade frequency, holding period,
sessions with exposure, position sizing -- while destroying any relationship
between its decisions and subsequent price.

Three designs were considered against that.

**Rotating the stream by a whole-session offset** is the cheapest and is a no-op
for this metric. Sortino is computed over per-session returns; rotating whole
sessions permutes that vector and leaves the ratio unchanged. Ruled out before
implementing it, which is why the brief asked for the check.

**Block-resampling the tick stream** destroys the most structure and also
destroys the thing that has to be preserved: a candidate keyed to intraday shape
trades at a different rate on a resampled path, so the null describes a different
strategy. Rejected for that reason, and the profile comparison is what would have
caught it.

**Circular rotation of each session's price path against the clock** is what this
implements. Within a session, the sequence of price *changes* is rotated by a
random offset and the path is rebuilt from the session's own open. So:

- **preserved**: the multiset of tick-to-tick moves, hence the session's realized
  volatility, its tick count, its trade arrival times, and the session's open.
  The candidate sees a path with the same microstructure and trades it at
  substantially the same rate.
- **destroyed**: the alignment between where in the session a move happened and
  what the clock said. Any edge that depends on time-of-day, on the session's
  overall drift arriving in a particular order, or on a signal preceding the move
  it predicts, is gone.

What it does *not* destroy is the session's total move: rotating a sequence of
increments leaves the sum unchanged, so the close-to-open return of the session
survives. That is deliberate. A design that also randomized the session's net
move would be testing "does this candidate have an edge, and separately, is this
session up or down", and the second question is not the candidate's fault. It is
also why a buy-and-hold candidate has *no* null distribution under this design --
its score is identical under every rotation -- and `candidate_null` reports that
as a degenerate null rather than as a p-value of 1/(R+1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import numpy as np

from ...types import Split, Tick


def rotate_session(ticks: list[Tick], offset: int) -> list[Tick]:
    """One session's ticks, with the price path rotated by `offset` increments.

    Timestamps, sizes, sequence numbers and the session's opening price are left
    exactly where they were; only the order of the price *increments* moves.
    """
    n = len(ticks)
    if n < 3:
        return list(ticks)
    prices = np.fromiter((t.price for t in ticks), dtype=float, count=n)
    deltas = np.diff(prices)
    k = offset % len(deltas)
    rebuilt = np.empty(n, dtype=float)
    rebuilt[0] = prices[0]
    rebuilt[1:] = prices[0] + np.cumsum(np.roll(deltas, k))
    out = []
    for tick, price in zip(ticks, rebuilt):
        out.append(Tick(ts=tick.ts, price=float(price), size=tick.size,
                        session=tick.session, seq=tick.seq, side=tick.side))
    return out


@dataclass
class RotatedDataset:
    """A dataset whose sessions are rotated, and nothing else about it changes.

    Wraps rather than subclasses, so the engine's sealed-dataset machinery, the
    calendar, the split enforcement and the holdout lock all behave exactly as
    they do for the real thing. The only method that differs is `ticks`.
    """

    inner: object
    seed: int

    @property
    def name(self) -> str:
        return f"{self.inner.name}:rot={self.seed}"

    def capabilities(self):
        return self.inner.capabilities()

    def calendar(self):
        return self.inner.calendar()

    def session_bounds(self, session: int):
        return self.inner.session_bounds(session)

    def __getattr__(self, name):
        """Everything else the engine asks for, forwarded.

        Explicit `ticks`, `digest` and `name` above; everything else -- the
        optional accessors different adapters happen to expose -- comes from the
        inner dataset unchanged. This is a *build-time analysis* wrapper that
        never reaches a strategy, so forwarding here is not the mistake it was
        in `_SealedDataset`: there is nothing on the far side of it to protect
        against.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.inner, name)

    def digest(self) -> str:
        return f"{self.inner.digest()}:rot={self.seed}"

    def ticks(self, split: Split, *, warmup: int = 0) -> Iterator[Tick]:
        """Session by session, so memory is one session rather than one split."""
        rng = np.random.default_rng(self.seed)
        current: list[Tick] = []
        session = None
        for tick in self.inner.ticks(split, warmup=warmup):
            if tick.session != session:
                if current:
                    yield from rotate_session(
                        current, int(rng.integers(0, max(len(current) - 1, 1))))
                current, session = [], tick.session
            current.append(tick)
        if current:
            yield from rotate_session(
                current, int(rng.integers(0, max(len(current) - 1, 1))))
