"""The dataset's own yardstick: what does trading it at RANDOM earn?

    table = build_table(days, spec)          # once per dataset+split
    draws = table.draws                      # every candidate is ranked on these

**What this is a null OF, and why it differs from the sign-flip.**

`round_turn.null_sample` holds the candidate's trade SELECTION fixed and flips
the signs of its outcomes, so it asks "given that you picked these positions,
was the direction skill?" That is a good question for a directional strategy
and the wrong one for a strategy whose edge is *which* positions it picked. An
arbitrageur does not choose the sign of a mispriced package; it chooses the
package. Picking the one mispriced box out of ~2,450 live quotes IS the edge,
and the sign-flip null cannot see it: measured on spx-arb-01, the winner sat at
p = 0.0004 against its own sign-flip null and beyond the entire support of this
one.

**One distribution per (dataset, split), shared by every trial.** That is the
property that matters for the correction. Per-candidate nulls are heterogeneous,
and on spx-arb-01 the `max_of_n` bar was set by trials with 15 downside sessions
whose Sortino denominators were unstable -- not by the winner, whose own null
gave p = 0.0004. A shared null cannot have that problem because there is one of
it, and the dependence between trials stops mattering for the same reason.

## The construction, and the detail that carries it

For each iteration, for each session:

1. draw an EVEN count of timestamps -- min 2, max 102, left-heavy, mean ~14;
2. place them uniformly across the session and sort;
3. the odd picks OPEN a position, long or short with equal probability, and the
   even picks CLOSE it;
4. score the per-session series with `scorer.score_returns`, the rule the gate
   uses.

**The count is load-bearing and it was got wrong twice before this.** At about
one round trip a session, commission dominates the gross price move, every draw
comes out negative, and the null degenerates into a cost floor: a candidate at
Sortino 0.001 and one at 5.74 both score p = 1/(b+1) because there is nothing
positive in the null to rank them against. At the specified ~7 round trips a
session the gross moves dominate instead, the distribution straddles zero
(measured: 37.8% of draws positive, median -0.0436, max +1.0688 over 4,000
iterations on the SPX path), and it discriminates -- 0.001 -> p = 0.3749,
1.00 -> p = 0.0005.

A null with no positive draws is a broken null, not a strict one. If `share_above_zero`
on a built table is near zero, the activity level is too low for the cost model
and the table must not be used.

**Measured robust to the one free parameter.** Across a mean count of 10 to 30
the 99th percentile moves only 0.344 -> 0.428, and FWER against a mismatched
candidate count stays within [0.037, 0.070]. It is still a parameter, so it is
pre-registered rather than defaulted silently.

**The limits, stated.** A random trader has a trading footprint of its own, and
matching a strategy on an activity level is not matching a strategy. Two
candidates with the same turnover can differ in ways this null cannot see. It
asks a better question than the sign-flip null, not a complete one -- and unlike
the sign-flip null it is not exact by construction, which is why the FWER table
in `scripts/correction_fwer.py` is the thing that licenses it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

MIN_TIMESTAMPS = 2
MAX_TIMESTAMPS = 102
DEFAULT_MEAN_TIMESTAMPS = 14.0


@dataclass(frozen=True, slots=True)
class TraderSpec:
    """Everything that decides the table. Hashed into its identity."""

    mean_timestamps: float = DEFAULT_MEAN_TIMESTAMPS
    # **How few trades a session may have.** 1 pair is the construction as
    # specified -- every session trades, nothing is left open at the close. But
    # it FLOORS activity at one round turn a session, and real candidates go
    # far below that: spx-arb-01's winner traded 0.03. A null cannot match an
    # activity it cannot reach, so the low buckets set this to 0 and let
    # Poisson leave sessions empty, which is what a rarely-trading candidate's
    # own return vector looks like.
    min_pairs: int = 1
    size: int = 1
    commission_per_contract: float = 0.10
    iterations: int = 4000
    seed: int = 17

    def digest(self) -> str:
        payload = json.dumps({k: getattr(self, k)
                              for k in sorted(self.__dataclass_fields__)},
                             sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class TraderTable:
    """The built distribution, plus what a reader needs to judge it."""

    draws: list[float] = field(default_factory=list)
    spec: dict = field(default_factory=dict)
    dataset: str = ""
    dataset_digest: str = ""
    split: str = ""
    generator: str = ""
    n_sessions: int = 0
    round_turns_per_session: float = 0.0
    share_above_zero: float = 0.0
    degenerate: bool = False
    note: str = ""

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _even_count(rng, mean: float, min_pairs: int = 1) -> int:
    """Even, left-heavy, at most MAX_TIMESTAMPS.

    Poisson on the number of PAIRS, so the count is even by construction rather
    than by rounding -- an odd count would leave a position open at the session
    close and the series would carry overnight risk the construction does not
    intend.

    `min_pairs=1` is the construction as specified and guarantees every session
    trades. `min_pairs=0` lets sessions come out empty, which the low-activity
    buckets need: a candidate trading 0.03 round turns a session has a mostly
    zero return vector, and a null that trades every session is not matched to
    it on the thing that matters.
    """
    pairs = int(rng.poisson(mean / 2.0))
    pairs = min(max(pairs, min_pairs), MAX_TIMESTAMPS // 2)
    return 2 * pairs


def price_path_draws(days: Sequence, spec: TraderSpec) -> tuple[list[float], float]:
    """Michael's construction on a price series. `days[s]` is session s's prices."""
    import numpy as np

    from ..scorer import score_returns

    rng = np.random.default_rng(spec.seed)
    n_sessions = len(days)
    arrays = [np.asarray(d, dtype=float) for d in days]
    out: list[float] = []
    turns: list[float] = []
    for _ in range(spec.iterations):
        per = np.empty(n_sessions)
        total = 0
        for s, px in enumerate(arrays):
            if px.size < 2:
                per[s] = 0.0
                continue
            n = _even_count(rng, spec.mean_timestamps, spec.min_pairs)
            if n == 0:
                per[s] = 0.0        # an untraded session is a real zero
                continue
            # Uniform ACROSS THE SESSION as a fraction, then mapped to a tick --
            # so the construction does not depend on how many ticks a session
            # happens to have.
            idx = np.minimum((np.sort(rng.random(n)) * px.size).astype(int),
                             px.size - 1)
            entry, exit_ = idx[0::2], idx[1::2]
            side = rng.integers(0, 2, size=entry.size) * 2 - 1
            gross = side * (px[exit_] - px[entry]) * spec.size
            cost = 2.0 * spec.commission_per_contract * spec.size * entry.size
            per[s] = float(gross.sum() - cost)
            total += entry.size
        out.append(score_returns(list(per)).primary)
        turns.append(total / max(n_sessions, 1))
    return out, (sum(turns) / len(turns) if turns else 0.0)


def build_table(days: Sequence, spec: TraderSpec, *, dataset: str = "",
                dataset_digest: str = "", split: str = "",
                generator: str = "price_path") -> TraderTable:
    draws, rt = price_path_draws(days, spec)
    finite = [d for d in draws if d == d and abs(d) != float("inf")]
    above = (sum(1 for d in finite if d > 0) / len(finite)) if finite else 0.0
    degenerate = above < 0.02
    return TraderTable(
        draws=finite, spec=spec.as_dict() if hasattr(spec, "as_dict") else {
            k: getattr(spec, k) for k in spec.__dataclass_fields__},
        dataset=dataset, dataset_digest=dataset_digest, split=split,
        generator=generator, n_sessions=len(days),
        round_turns_per_session=rt, share_above_zero=above,
        degenerate=degenerate,
        note=("DEGENERATE: fewer than 2% of draws are positive, so commission "
              "dominates the gross move at this activity level and the table "
              "cannot rank anything above zero. Raise mean_timestamps or do "
              "not use it." if degenerate else
              "one distribution per dataset and split; every trial in a "
              "campaign is ranked against these same draws"))


# **How many table draws a campaign of N trials needs, and why it is checked.**
#
# The `max_of_n` bar at N trials is the upper tail of the maximum of N draws,
# and a bootstrap maximum cannot exceed the largest value the table happens to
# contain. Worse, a COMMITTED table is one realisation shared by every campaign
# that uses it, so whatever bias its extreme tail carries does not average out
# across campaigns the way a per-campaign null's would.
#
# Measured on the SPX path, 120 campaigns per cell, candidate matched to the
# table:
#
#     N      table      FWER     95% CI
#      20    6,000     0.057   [0.031, 0.083]
#      50    6,000     0.043   [0.020, 0.066]
#     400    6,000     0.092   [0.040, 0.143]
#     400   20,000     0.067   [0.022, 0.111]
#     400   60,000     0.075   [0.028, 0.122]
#
# Calibrated at N <= 50. At N = 400 the point estimate sits above q at every
# table size tried and a bigger table does not fix it -- so that regime is
# REFUSED rather than documented, because a correction nobody has shown to
# control its error rate is not a correction.
MIN_DRAWS_PER_TRIAL = 120
MAX_SUPPORTED_TRIALS = 50
# **Below this, a MATCHED bucket still over-rejects, so it is refused.**
#
# Measured on NQ train, candidate at 0.03 round turns a session, fresh
# candidates every rep across three table realisations, N = 50:
#
#     one shared table at 7 rt/session    FWER 0.673   <- the unbucketed case
#     its own bucket, matched             FWER 0.082
#
# Bucketing is the difference between 0.673 and 0.082, which is the whole
# argument for it. But 0.082 is still above q = 0.05, and the cause is not the
# null: at 0.03 round turns a session roughly one session in thirty trades, so
# Sortino is computed on a near-empty return series and is severely discrete.
# A discrete statistic over-rejects against any null. It is the same pathology
# as the -0.775 correlation between downside-session count and per-trial null
# tail weight.
#
# At 7 round turns a session a matched table measures 0.042 [0.027, 0.060], so
# the method is calibrated in the active regime. Where the boundary sits
# between 0.03 and 7 is NOT measured, so the floor is set at 1.0 -- the point
# below which the construction needs `min_pairs = 0` and sessions start coming
# out empty. That is a conservative guess at a boundary, flagged as such.
MIN_SUPPORTED_ACTIVITY = 1.0


# ---- activity buckets ----------------------------------------------------- #
# **Why buckets, and the argument that survives the measurement noise.**
#
# One fixed table cannot be a null for every candidate, because activity spans
# two orders of magnitude: spx-arb-01's winner traded 0.03 round turns a
# session against a default table's 7. That is not a tolerance question, it is
# a reachability one -- and the construction as specified (min 2 timestamps a
# session) cannot even GENERATE activity below 1 round turn a session, which is
# why `min_pairs` exists.
#
# The FWER cost of mismatch is real but modest, and smaller than I first
# reported. Measured on NQ train with fresh candidates every rep across four
# table realisations, N = 50:
#
#     exact match     mean 0.042   range [0.027, 0.060]
#     0.7x activity   mean 0.055   range [0.047, 0.080]
#
# An earlier single-table cell read 0.135 at 0.7x and a pooled-candidate sweep
# read 0.100 at exact match; both were measurement artefacts -- one table taken
# as the answer, and correlated reps given a binomial interval. The numbers
# above are the ones to trust, and they say a 0.7x mismatch costs about 0.013
# of FWER. So the bucket factor is chosen for reachability first and tolerance
# second.
BUCKET_FACTOR = 1.3
BUCKET_MIN = 0.01          # round turns per session
BUCKET_MAX = 60.0


def bucket_index(turns_per_session: float) -> int:
    """Which geometric bucket an activity level falls in.

    Geometric, not linear: the distance that matters between 0.03 and 0.04 is
    the same as between 3 and 4, because what degrades the null is the RATIO.
    """
    import math

    x = min(max(float(turns_per_session), BUCKET_MIN), BUCKET_MAX)
    return int(math.floor(math.log(x / BUCKET_MIN) / math.log(BUCKET_FACTOR)))


def bucket_activity(index: int) -> float:
    """The representative activity of a bucket: its geometric centre."""
    import math

    lo = BUCKET_MIN * (BUCKET_FACTOR ** index)
    return lo * math.sqrt(BUCKET_FACTOR)


def spec_for_activity(turns_per_session: float, *, iterations: int,
                      commission_per_contract: float, size: int = 1,
                      seed: int = 17) -> TraderSpec:
    """A spec whose random trader trades at `turns_per_session`.

    One round turn is two timestamps, so the mean count is twice the target.
    `min_pairs` drops to 0 below one round turn a session -- a table that
    trades every session cannot match a candidate that trades one session in
    thirty, and clamping to 1 pair would silently floor the whole low end.
    """
    target = bucket_activity(bucket_index(turns_per_session))
    return TraderSpec(mean_timestamps=2.0 * target,
                      min_pairs=1 if target >= 1.0 else 0,
                      size=size, commission_per_contract=commission_per_contract,
                      iterations=iterations, seed=seed)


# How far a candidate's activity may sit from the table's. A 0.7x mismatch
# costs about 0.013 of FWER (0.055 against 0.042 matched), so the tolerance is
# NOT what the tight bucket factor is for -- reachability is. Within a bucket
# the worst case is the full factor, so the guard allows it and nothing wider. A candidate outside its own bucket means the wrong table was
# loaded, which is a bug rather than a tolerance to absorb.
ACTIVITY_TOLERANCE = BUCKET_FACTOR - 1.0


def check_supported(table: TraderTable, n_trials: int,
                    candidate_turns_per_session: float | None = None) -> None:
    """Refuse a campaign this table cannot license. Raises, never warns."""
    if table.degenerate:
        raise ValueError(
            f"this table is degenerate ({table.share_above_zero:.1%} of draws "
            f"positive): commission dominates the gross move at "
            f"{table.round_turns_per_session:.1f} round turns per session, so "
            f"it cannot rank anything above zero. Raise mean_timestamps.")
    if n_trials > MAX_SUPPORTED_TRIALS:
        raise ValueError(
            f"the random-trader null is calibrated to N <= {MAX_SUPPORTED_TRIALS} "
            f"and this campaign has {n_trials} trials. Measured FWER at N = 400 "
            f"is 0.067-0.092 against q = 0.05 at every table size from 6,000 to "
            f"60,000 draws, so the large-N regime is not licensed. Register "
            f"null_source: round_turn_mc for a campaign this size, or measure "
            f"the cell first with scripts/correction_fwer.py.")
    if candidate_turns_per_session is not None:
        if candidate_turns_per_session < MIN_SUPPORTED_ACTIVITY:
            raise ValueError(
                f"the candidate trades {candidate_turns_per_session:.3f} round "
                f"turns a session, below the {MIN_SUPPORTED_ACTIVITY} this null "
                f"is calibrated for. Measured at 0.03 with a MATCHED bucket, "
                f"FWER is 0.082 against q = 0.05 -- not because the null is "
                f"wrong but because Sortino on a series where one session in "
                f"thirty trades is too discrete to test. (Unbucketed it is "
                f"0.673, which is why buckets exist.) Register null_source: "
                f"round_turn_mc, which conditions on the candidate's own "
                f"trades and does not care how few there are.")
        ref = table.round_turns_per_session
        if ref > 0:
            ratio = candidate_turns_per_session / ref
            if abs(ratio - 1.0) > ACTIVITY_TOLERANCE:
                raise ValueError(
                    f"the candidate trades {candidate_turns_per_session:.3f} "
                    f"round turns a session against this table's {ref:.3f} "
                    f"({ratio:.2f}x). A shared table is only a valid null at "
                    f"matched activity, because activity changes the "
                    f"per-session variance structure and not just the cost "
                    f"drag. Within a bucket that costs little (0.055 at 0.7x "
                    f"against 0.042 matched); across two orders of magnitude "
                    f"it is ruinous (0.673 for a 0.03-round-turn candidate "
                    f"against a 7-round-turn table). A candidate this far out "
                    f"means the wrong bucket was loaded. Build a table at this "
                    f"activity with spec_for_activity(), or register "
                    f"null_source: round_turn_mc, which is exact for any "
                    f"candidate because it uses the candidate's own trades.")
    need = MIN_DRAWS_PER_TRIAL * n_trials
    if len(table.draws) < need:
        raise ValueError(
            f"{len(table.draws)} draws for {n_trials} trials. A bootstrap "
            f"maximum cannot exceed the table's own largest value, so the bar "
            f"is biased low when the table is small relative to N; this needs "
            f"at least {need}.")


def p_value(table: TraderTable, observed: float) -> dict:
    """Where `observed` falls in the table. The same shape `max_of_n` returns."""
    import numpy as np

    d = np.asarray(table.draws, dtype=float)
    if d.size == 0:
        return {"p": 1.0, "n_draws": 0, "note": "empty table"}
    exceed = int((d >= observed).sum())
    b = d.size
    return {
        "p": (1 + exceed) / (b + 1),
        "n_draws": b,
        "exceedances": exceed,
        "p_floor": 1.0 / (b + 1),
        "p_is_bound": exceed == 0,
        "null_support_max": float(d.max()),
        "beyond_null_support": observed > float(d.max()),
        "margin_over_support": observed - float(d.max()),
        "quantiles": {str(q): float(np.quantile(d, q))
                      for q in (0.5, 0.9, 0.99)},
        "share_above_zero": table.share_above_zero,
        "degenerate": table.degenerate,
    }


def write_table(path: Path, table: TraderTable) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = table.as_dict()
    payload = json.dumps(body, sort_keys=True)
    body["digest"] = hashlib.sha256(payload.encode()).hexdigest()[:16]
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    return body["digest"]


def read_table(path: Path) -> TraderTable:
    body = json.loads(path.read_text())
    body.pop("digest", None)
    return TraderTable(**body)
