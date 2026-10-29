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

from .dataset import Calendar, HoldoutLock, SplitBounds, sessions_for
from ..types import NS_PER_SECOND, DataCapabilities, Quote, Side, Split, Tick, Ts

SESSION_ORIGIN_NS = 1_700_000_000 * NS_PER_SECOND  # arbitrary, fixed, UTC

# A fixed stream id for the schedule, kept distinct from the per-session
# streams so adding an irregularity never disturbs a session's contents.
SCHEDULE_STREAM = 0x5C_4ED


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

    # ---- irregularity ----------------------------------------------------- #
    # Real markets are not uniform, and every real-data defect found so far was
    # invisible here for the same reason: this generator was. Sessions all the
    # same length hid a strategy that could not handle a half day; every interval
    # having ticks hid a session length learned as a bar count; sessions starting
    # at the same price hid overnight holds realising the previous day's drift.
    # Each of those cost a task to find on real data.
    #
    # So the default is irregular, and `uniform=True` is the opt-in for tests
    # that want determinism from simplicity. Inheriting the easy world by default
    # is what made it a blind spot.
    uniform: bool = False
    # Some sessions close early, without warning, the way a half day does.
    early_close_rate: float = 0.05
    early_close_frac: float = 0.45
    # Normal sessions are not identical either.
    length_jitter: float = 0.04
    # Runs of intervals with no trade. The mean run is longer than one 5m bar at
    # 30s spacing, so genuinely empty bars occur rather than merely thin ones.
    empty_gap_rate: float = 0.012
    empty_gap_mean_ticks: float = 18.0
    # Quietness varies by session as much as volatility does, and inversely: the
    # calm days are the thin ones. Without this every session has roughly the
    # same bar count and a length learned from bar counts still looks stable --
    # which is precisely the defect that has to become visible.
    quiet_sd: float = 0.8
    # Volatility clusters across sessions: an AR(1) on log volatility. NQ's
    # session returns show lag-1 |r| autocorrelation of +0.22 against a +/-0.10
    # band, and an i.i.d. generator has none at all.
    vol_cluster_rho: float = 0.55
    vol_cluster_sd: float = 0.35
    # A top of book beside each trade. Off by default, and absent from the
    # digest when off, so a spec that does not ask for quotes hashes exactly as
    # it did before the field existed.
    quotes: bool = False

    @property
    def irregular(self) -> bool:
        return not self.uniform

    def as_dict(self) -> dict:
        out = {k: getattr(self, k) for k in self.__dataclass_fields__ if k != "quotes"}
        if self.quotes:
            out["quotes"] = True
        return out


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
        self._schedule()

    # ---- Dataset protocol ------------------------------------------------- #
    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(
            has_trades=True, has_quotes=self.spec.quotes, has_book=False
        )

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
        return self._starts[session], self._ends[session]

    def early_close_dates(self) -> frozenset[int]:
        """Sessions that close early, by index. The same surface the NQ adapter
        exposes, so anything consuming it works on either dataset."""
        return self._early

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

    def scored_sessions(self, split: Split, warmup: int = 0) -> list[int]:
        """The actionable sessions of a split -- the same definition `nq.py`
        uses, from the same `sessions_for` helper this class already calls.

        **Added 2026-10-05 because its absence made the null path untestable.**
        `nq_campaign.stage_nulls` reassembles the sign-flip null onto a session
        calendar and asks the dataset for it; only the NQ dataset had the
        method, so the whole correction could be exercised on real market data
        or not at all. The first synthetic session rehearsal stopped here. It is
        not in the `Dataset` protocol, which is why the gap went unnoticed --
        worth knowing, and left as a separate question from filling it.
        """
        _, live = sessions_for(self._calendar, split, warmup)
        return list(live)

    def quotes(self, split: Split, *, warmup: int = 0) -> Iterator[Quote]:
        """One top of book at each trade's timestamp, one tick either side.

        The trade itself prints at the mid, inside the spread, so a queue model
        does not fill on it unless a later trade goes through. That is a
        generator choice, not a claim about how a book works; a test that needs
        a trade at the touch builds its own quotes.
        """
        if not self.spec.quotes:
            return iter(())
        self._lock.check(split)
        warm, live = sessions_for(self._calendar, split, warmup)
        half = self.spec.tick_size

        def _gen() -> Iterator[Quote]:
            for session in list(warm) + list(live):
                for i, tick in enumerate(self._session_ticks(session)):
                    yield Quote(
                        ts=tick.ts,
                        bid_price=tick.price - half,
                        bid_size=1 + (i % 5),
                        ask_price=tick.price + half,
                        ask_size=1 + ((i + 2) % 5),
                        session=session,
                    )

        return _gen()


    # ---- schedule ---------------------------------------------------------- #
    def _schedule(self) -> None:
        """Session lengths, bounds and volatility, fixed up front.

        Precomputed rather than drawn per session, for the same reason session
        contents are seeded per session: serving one split in isolation has to
        give exactly the data a full walk would. A volatility process that
        depends on the previous session cannot be drawn from a per-session seed,
        so the whole series is generated once, deterministically, from the
        dataset seed.
        """
        s = self.spec
        n = s.n_sessions
        base = s.ticks_per_session
        spacing = s.tick_spacing_ns

        if s.uniform:
            self._lengths = [base] * n
            self._early: frozenset[int] = frozenset()
            self._vol_mult = [1.0] * n
            self._quiet_mult = [1.0] * n
        else:
            rng = np.random.default_rng([s.seed, SCHEDULE_STREAM])
            early = rng.random(n) < s.early_close_rate
            jitter = rng.normal(0.0, s.length_jitter, size=n)
            lengths = np.maximum(
                20,
                np.rint(
                    base
                    * np.where(early, s.early_close_frac, 1.0)
                    * (1.0 + jitter)
                ),
            ).astype(int)
            self._lengths = [int(x) for x in lengths]
            self._early = frozenset(int(i) for i in np.flatnonzero(early))

            # AR(1) on log volatility, started at its stationary mean so the
            # first sessions are not systematically calmer than the rest.
            rho, sd = s.vol_cluster_rho, s.vol_cluster_sd
            innov = rng.normal(0.0, sd, size=n)
            log_v = np.empty(n)
            prev = 0.0
            for i in range(n):
                prev = rho * prev + innov[i]
                log_v[i] = prev
            # Centred so the average session has the volatility the spec asks
            # for; clustering changes the ordering, not the overall scale.
            self._vol_mult = [float(v) for v in np.exp(log_v - (sd**2) / (2 * (1 - rho**2)))]
            # Thinness is the mirror of volatility, plus its own noise.
            quiet = np.exp(-log_v * 0.5 + rng.normal(0.0, s.quiet_sd, size=n))
            self._quiet_mult = [float(min(q, 4.0)) for q in quiet]

        starts, ends = [], []
        t = SESSION_ORIGIN_NS
        for k in range(n):
            starts.append(t)
            # The session's nominal span is its full length, whether or not every
            # interval in it produced a trade.
            ends.append(t + self._lengths[k] * spacing)
            t = ends[-1] + s.session_gap_ns
        self._starts, self._ends = starts, ends

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
        n = self._lengths[session]
        start_ts, _ = self.session_bounds(session)

        noise = rng.normal(0.0, s.vol_per_tick * self._vol_mult[session], size=n)
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

        keep = self._observed(rng, n, self._quiet_mult[session])
        out = [
            Tick(
                ts=start_ts + i * s.tick_spacing_ns,
                price=float(prices[i]),
                size=int(sizes[i]),
                session=session,
                side=sides[i],
            )
            for i in range(n)
            if keep[i]
        ]
        self._cache[session] = out
        return out

    def _observed(self, rng, n: int, quiet: float = 1.0) -> np.ndarray:
        """Which ticks are seen. Runs of unseen ones become empty intervals.

        The price path is generated over every tick and then thinned, which is
        the right way round: the market moves during a quiet stretch, you just do
        not see it happen. A generator that simply spaced its ticks further apart
        would produce thin bars rather than absent ones, and the defect this is
        here to catch -- a session length learned from a bar *count* -- needs bars
        that genuinely do not exist.

        The first and last ticks are always kept, so a session still has an open
        and a close.
        """
        keep = np.ones(n, dtype=bool)
        s = self.spec
        if s.uniform or s.empty_gap_rate <= 0 or n < 4:
            return keep
        starts = np.flatnonzero(rng.random(n) < min(s.empty_gap_rate * quiet, 0.2))
        if starts.size:
            runs = rng.geometric(1.0 / max(s.empty_gap_mean_ticks, 1.0), size=starts.size)
            for begin, run in zip(starts, runs):
                keep[begin : begin + int(run)] = False
        keep[0] = keep[-1] = True
        return keep
