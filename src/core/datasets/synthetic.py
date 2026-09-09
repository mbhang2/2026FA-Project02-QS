"""A tick generator with a switch for how much signal is in it.

Built before the first real adapter on purpose. Two implementations from day one
is what keeps the `Dataset` interface honest, and it makes null and power
calibration ordinary runs rather than special cases:

* `signal_strength = 0` is pure noise. The gate must accept essentially nothing.
* `signal_strength > 0` plants an edge of known size. The gate must find it, and
  sweeping the strength gives a power curve -- how large an edge has to be before
  this harness can see it.

The planted edge is deliberately the simplest thing a strategy can express:
within a session, the drift of each interval is `signal_strength` times the sign
of the previous interval's return. A momentum rule sees it; nothing else does.
Generating ticks rather than bars means synthetic data exercises the same
aggregation path as a real feed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterator

import numpy as np

from ..dataset import Calendar, HoldoutLock, SplitBounds, sessions_for
from ..types import NS_PER_SECOND, DataCapabilities, Side, Split, Tick, Ts

SESSION_ORIGIN_NS = 1_700_000_000 * NS_PER_SECOND  # arbitrary, fixed, UTC


@dataclass(frozen=True)
class SyntheticSpec:
    n_sessions: int = 120
    ticks_per_session: int = 780          # e.g. 6.5h of one tick per 30s
    tick_spacing_ns: int = 30 * NS_PER_SECOND
    session_gap_ns: int = 17 * 3600 * NS_PER_SECOND
    start_price: float = 100.0
    # Sessions used to restart at start_price, which put a discontinuity at every
    # boundary: a position carried across it realized the whole previous
    # session's drift. Measured at signal 1.0, same-session trades averaged +1.30
    # and overnight ones -16.78. Prices now carry forward, with an optional
    # overnight gap, so the process is continuous the way a real one is.
    continuous_sessions: bool = True
    overnight_gap_vol: float = 0.0
    tick_size: float = 0.25
    vol_per_tick: float = 0.02            # sd of the per-tick noise, in price units
    signal_strength: float = 0.0          # 0 = pure noise
    signal_interval_ticks: int = 10       # the interval the momentum acts over
    seed: int = 0
    train_frac: float = 0.6
    validation_frac: float = 0.2
    embargo_sessions: int = 2

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class SyntheticDataset:
    """`Dataset` implementation. Deterministic in `spec.seed` alone."""

    def __init__(self, spec: SyntheticSpec | None = None, **overrides):
        self.spec = spec or SyntheticSpec(**overrides)
        if overrides and spec is not None:
            raise TypeError("pass a SyntheticSpec or keyword overrides, not both")
        self.name = f"synthetic:s={self.spec.signal_strength:g}:seed={self.spec.seed}"
        self._lock = HoldoutLock()
        self._calendar = self._build_calendar()
        self._cache: dict[int, list[Tick]] = {}

    # ---- Dataset protocol ------------------------------------------------- #
    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(has_trades=True, has_quotes=False, has_book=False)

    def calendar(self) -> Calendar:
        return self._calendar

    def digest(self) -> str:
        """Pins the exact data a run used. Every ledger row carries it, so a
        trial is reproducible against the bytes it actually ran on rather than
        against whatever the generator produces after someone edits a default."""
        payload = json.dumps(
            {"generator": "synthetic/1", **self.spec.as_dict()}, sort_keys=True
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def unlock_holdout(self, reason: str) -> None:
        self._lock.unlock(reason)

    @property
    def holdout_lock(self) -> HoldoutLock:
        return self._lock

    def session_bounds(self, session: int) -> tuple[Ts, Ts]:
        start = SESSION_ORIGIN_NS + session * (
            self.spec.ticks_per_session * self.spec.tick_spacing_ns
            + self.spec.session_gap_ns
        )
        return start, start + self.spec.ticks_per_session * self.spec.tick_spacing_ns

    def ticks(self, split: Split, *, warmup: int = 0) -> Iterator[Tick]:
        """Warmup sessions come first and are marked by the engine as
        non-actionable; the strategy sees their bars but cannot trade them."""
        self._lock.check(split)
        warm, live = sessions_for(self._calendar, split, warmup)
        for session in list(warm) + list(live):
            yield from self._session_ticks(session)

    def warmup_sessions(self, split: Split, warmup: int) -> set[int]:
        warm, _ = sessions_for(self._calendar, split, warmup)
        return set(warm)

    # ---- generation -------------------------------------------------------- #
    def _build_calendar(self) -> Calendar:
        s = self.spec
        n = s.n_sessions
        emb = s.embargo_sessions
        train_stop = int(n * s.train_frac)
        val_start = train_stop + emb
        val_stop = val_start + int(n * s.validation_frac)
        hold_start = val_stop + emb
        if hold_start >= n:
            raise ValueError(
                f"{n} sessions cannot hold a {s.train_frac:.0%}/{s.validation_frac:.0%} "
                f"split with a {emb}-session embargo on each side"
            )
        return Calendar(
            train=SplitBounds(0, train_stop),
            validation=SplitBounds(val_start, val_stop),
            holdout=SplitBounds(hold_start, n),
            embargo_sessions=emb,
            n_sessions=n,
        )

    def _session_open(self, session: int) -> float:
        """Where a session starts.

        Recursive by construction and memoised through the tick cache: session k
        opens where session k-1 closed, plus an optional overnight gap. The
        recursion is why `ticks()` for a late split still produces the same
        prices as a full walk -- the opens are chained, not sampled.
        """
        if session <= 0 or not self.spec.continuous_sessions:
            return self.spec.start_price
        prev = self._session_ticks(session - 1)
        close = prev[-1].price if prev else self.spec.start_price
        if self.spec.overnight_gap_vol:
            g = np.random.default_rng([self.spec.seed, session, 7]).normal(
                0.0, self.spec.overnight_gap_vol
            )
            close += float(g)
        return close

    def _session_ticks(self, session: int) -> list[Tick]:
        """Cached per session so a warmup replay and the live walk see identical
        bytes, and so two runs at the same seed are byte-identical."""
        hit = self._cache.get(session)
        if hit is not None:
            return hit

        s = self.spec
        # Seeded per session, not per dataset: a session's contents then do not
        # depend on which other sessions were drawn first, so serving a split in
        # isolation gives the same data as serving all of them.
        rng = np.random.default_rng([s.seed, session])
        n = s.ticks_per_session
        start_ts, _ = self.session_bounds(session)

        noise = rng.normal(0.0, s.vol_per_tick, size=n)
        drift = np.zeros(n)
        if s.signal_strength:
            k = s.signal_interval_ticks
            # Momentum over consecutive k-tick intervals: the drift applied to
            # interval i is signal_strength * sign(return of interval i-1).
            for i in range(k, n, k):
                prev = noise[i - k : i].sum() + drift[i - k : i].sum()
                if prev:
                    drift[i : i + k] = s.signal_strength * np.sign(prev) / k

        path = self._session_open(session) + np.cumsum(noise + drift)
        # Round to the tick grid: a simulator that fills at unquantised prices
        # flatters any strategy whose edge is smaller than a tick.
        prices = np.round(path / s.tick_size) * s.tick_size
        sizes = rng.integers(1, 5, size=n)

        # Aggressor from the tick rule: an uptick is a buy, a downtick a sell,
        # an unchanged trade inherits the last classification. Not a claim about
        # market microstructure -- it is a stand-in that makes order-flow
        # features non-degenerate on synthetic data, and the real adapter has the
        # field from the feed.
        sides: list[Side | None] = []
        last = None
        for i in range(n):
            if i and prices[i] > prices[i - 1]:
                last = Side.BUY
            elif i and prices[i] < prices[i - 1]:
                last = Side.SELL
            sides.append(last)

        out = [
            Tick(
                ts=start_ts + i * s.tick_spacing_ns,
                price=float(prices[i]),
                size=int(sizes[i]),
                session=session,
                side=sides[i],
            )
            for i in range(n)
        ]
        self._cache[session] = out
        return out
