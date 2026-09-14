"""The per-candidate null: run this candidate against R rotations of its own data.

**The resolution floor is the first thing to understand about it.** An empirical
null built from R rotations cannot produce a p-value below `1/(R+1)`, because the
observed score is one of the R+1 values being ranked. That floor is reported *as
a floor* -- `p_floor` and `at_floor` are fields, and `p` is never a smaller
number substituted for one that could not be measured.

Task 30's two BH discoveries were partly manufactured by exactly that
substitution. The tabulated grid bottoms out at 0.0005, so every score past the
last quantile reported exactly 0.0005, and two of them tied at the rank-2 BH
critical value of 0.0005. A clipped value used as a value is a discovery
manufactured by the reporting convention.

**The behavioural profile is the validity check, not decoration.** The null
hypothesis is "this candidate, with its trading behaviour unchanged, has no
edge". If the candidate trades at a different rate on rotated data, the null
being computed is some other strategy's. `profile_of` measures it on both and
`CandidateNull.profile_drift` is the comparison; a large drift invalidates the
null rather than merely being noted.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Callable, Sequence

from ...types import Split
from .rotation import RotatedDataset


@dataclass(frozen=True, slots=True)
class Profile:
    """What "this candidate's trading behaviour" means, operationally."""

    trades: int
    fills: int
    sessions_with_exposure: int
    mean_position_size: float
    median_holding_ns: float
    return_sd: float

    def as_dict(self) -> dict:
        return {
            "trades": self.trades, "fills": self.fills,
            "sessions_with_exposure": self.sessions_with_exposure,
            "mean_position_size": self.mean_position_size,
            "median_holding_ns": self.median_holding_ns,
            "return_sd": self.return_sd,
        }

    def drift_from(self, other: "Profile") -> dict:
        """Relative change per field, so "the profile moved" is a number."""
        out = {}
        for key, mine in self.as_dict().items():
            theirs = other.as_dict()[key]
            base = max(abs(theirs), 1e-9)
            out[key] = (mine - theirs) / base
        return out


def profile_of(engine, returns: Sequence[float]) -> Profile:
    from ..metrics import _holding_periods

    rows = engine.reconstructed().rows
    holds = _holding_periods(rows)
    exposure = sum(1 for v in engine.record.end_of_session_positions.values() if v)
    return Profile(
        trades=sum(1 for r in rows if r.realized_pnl != 0.0),
        fills=len(rows),
        # Sessions the candidate ended holding something. A cheap, honest proxy
        # for "was it in the market", and the one the record already carries.
        sessions_with_exposure=exposure,
        mean_position_size=(
            statistics.fmean([abs(r.size) for r in rows]) if rows else 0.0),
        median_holding_ns=float(statistics.median(holds)) if holds else 0.0,
        return_sd=statistics.stdev(returns) if len(returns) > 1 else 0.0,
    )


@dataclass
class CandidateNull:
    r: int
    observed: float
    null_scores: list[float] = field(default_factory=list)
    observed_profile: Profile | None = None
    null_profiles: list[Profile] = field(default_factory=list)
    wall_clock_s: float = 0.0

    @property
    def p_floor(self) -> float:
        """`1/(R+1)`. Nothing smaller is measurable with R rotations."""
        return 1.0 / (self.r + 1)

    @property
    def p(self) -> float:
        """The randomization p-value, with the observed score in the ranking.

        `(1 + #{null >= observed}) / (R + 1)` -- the standard construction, and
        the +1 in the numerator is what makes it valid rather than optimistic.
        """
        exceed = sum(1 for s in self.null_scores if s >= self.observed)
        return (1 + exceed) / (self.r + 1)

    @property
    def at_floor(self) -> bool:
        """The p-value is the floor, which means it is a bound, not a value."""
        return self.p <= self.p_floor + 1e-12

    @property
    def degenerate(self) -> bool:
        """Every rotation gave the same score, so the design does not randomize
        anything this candidate is sensitive to. Buy-and-hold is the clear case:
        rotating increments leaves the session's net move unchanged."""
        if len(self.null_scores) < 2:
            return True
        return statistics.pstdev(self.null_scores) < 1e-12

    @property
    def profile_drift(self) -> dict:
        if self.observed_profile is None or not self.null_profiles:
            return {}
        median = Profile(
            trades=int(statistics.median([p.trades for p in self.null_profiles])),
            fills=int(statistics.median([p.fills for p in self.null_profiles])),
            sessions_with_exposure=int(statistics.median(
                [p.sessions_with_exposure for p in self.null_profiles])),
            mean_position_size=statistics.median(
                [p.mean_position_size for p in self.null_profiles]),
            median_holding_ns=statistics.median(
                [p.median_holding_ns for p in self.null_profiles]),
            return_sd=statistics.median([p.return_sd for p in self.null_profiles]),
        )
        return median.drift_from(self.observed_profile)

    @property
    def profile_preserved(self) -> bool:
        """Within 25% on every field. A threshold, stated rather than eyeballed:
        past it the null is some other strategy's and the p-value means nothing.
        """
        drift = self.profile_drift
        return bool(drift) and all(abs(v) <= 0.25 for v in drift.values())

    def quantile(self, far: float) -> float:
        """The score a candidate would need for a stated rate, from its own null."""
        if not self.null_scores:
            return float("nan")
        ordered = sorted(self.null_scores, reverse=True)
        k = max(0, min(len(ordered) - 1, int(math.ceil(far * len(ordered))) - 1))
        return ordered[k]

    def as_dict(self) -> dict:
        return {
            "r": self.r, "observed": self.observed, "p": self.p,
            "p_floor": self.p_floor, "at_floor": self.at_floor,
            "degenerate": self.degenerate,
            "profile_preserved": self.profile_preserved,
            "profile_drift": self.profile_drift,
            "observed_profile": (self.observed_profile.as_dict()
                                 if self.observed_profile else None),
            "null_mean": statistics.fmean(self.null_scores) if self.null_scores else None,
            "null_sd": (statistics.stdev(self.null_scores)
                        if len(self.null_scores) > 1 else None),
            "wall_clock_s": round(self.wall_clock_s, 2),
        }


def candidate_null(
    strategy_path,
    dataset_factory: Callable[[], object],
    *,
    class_name: str = "Strategy",
    r: int = 200,
    split: Split = Split.TRAIN,
    warmup_sessions: int = 0,
    fill_model: str = "unfavorable",
    seed: int = 1,
    seed0: int = 90_000,
    engine_kwargs: dict | None = None,
) -> CandidateNull:
    """Run the candidate once on real data and `r` times on rotations of it.

    **The candidate runs behind the process boundary, every time.** This is the
    cost the brief did not price: per-candidate randomization multiplies the
    isolation overhead by R, and running it in process to save that would reopen
    findings 2 and 3 for every candidate in a campaign. A fresh child per run
    also gets the "fits again on each rotation" property for free -- a strategy
    that fitted anything in `setup` cannot carry it across, because the process
    that fitted it is gone.

    `strategy_path` is a file, not a factory, for the same reason: what crosses
    the boundary is source.
    """
    import time
    from pathlib import Path

    from ...engine import Engine
    from ...engine.remote import RemoteStrategy
    from ...runlog import RunTicket
    from ..metrics import session_returns
    from ..scorer import score

    ticket = RunTicket.for_null_calibration(
        f"per-candidate rotation null, R={r}")
    kwargs = dict(split=split, warmup_sessions=warmup_sessions,
                  fill_model=fill_model, seed=seed, **(engine_kwargs or {}))
    path = Path(strategy_path)

    def run(dataset):
        with RemoteStrategy(path, class_name) as strategy:
            engine = Engine(dataset, strategy, ticket=ticket, **kwargs)
            engine.run()
        blotter = engine.reconstructed()
        period = engine.session_calendar()
        returns = session_returns(blotter, period)
        return score(blotter, period=period), profile_of(engine, returns)

    t0 = time.monotonic()
    observed, observed_profile = run(dataset_factory())
    nulls, profiles = [], []
    for i in range(r):
        s, prof = run(RotatedDataset(dataset_factory(), seed=seed0 + i))
        # A rejected rotation contributes the reject sentinel, which never
        # exceeds an observed score. Dropping it would shrink R and inflate the
        # p-value's denominator claim.
        nulls.append(s.primary if not s.rejected else float("-inf"))
        profiles.append(prof)

    return CandidateNull(
        r=r,
        observed=observed.primary if not observed.rejected else float("-inf"),
        null_scores=nulls, observed_profile=observed_profile,
        null_profiles=profiles, wall_clock_s=time.monotonic() - t0,
    )
