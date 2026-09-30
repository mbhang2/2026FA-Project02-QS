"""A candidate's null from its own round turns: flip the direction, keep the cost.

**Pre-registered in `docs/design/pre-registration.md` section 3b**, and that
document is the specification this file implements rather than the other way
round. If the implementation disagrees with it, the implementation is wrong.

## What it does

A **round turn** is a position opened from flat and carried back to flat. For each
replication, every round turn's *gross* P&L is multiplied by -1 with probability
0.5, commission is charged again, the results are reassembled into per-session
returns on the real session calendar, and the same `sortino_per_session` the gate
uses is computed. `B = 10_000` replications give that candidate's null sample.

## Why sign-flip, and not permutation or a bootstrap

"Monte Carlo over round turns" names at least three different nulls and they give
different answers, so leaving the choice open would have left a free parameter for
somebody to close after seeing a result. Sign-flip preserves everything about the
strategy's *activity* -- how often it traded, when, at what size, and what it paid
-- and destroys only whether it was right. That is the closest analogue to "this
candidate has no directional edge", which is what the gate asks.

Permutation additionally destroys session structure, which the metric is defined
over. A bootstrap answers a narrower question about sampling error.

## Why commission is re-applied rather than flipped with the P&L

A round turn that lost 0.5 points net *paid commission to do it*. Flipping the net
figure hands the null a 0.5-point gain that never paid to trade, which makes the
null too generous and the gate too easy -- in the direction that manufactures
discoveries. So the sign lands on the gross and the cost is charged again:

    replicated = sign * gross - cost          sign in {-1, +1}

## What this null cannot see

**It conditions on the round turns the strategy actually took.** It cannot detect a
candidate that got lucky in *which* trades it entered, because those trades are
held fixed. `rotation.py` re-runs the strategy against altered data and can. That
is the difference between seconds and hours, and it is why a result worth claiming
is re-run under the conditional null rather than claimed from this one.

## It tests directional skill, NOT profitability -- and that is a trap

**Measured while building this.** Two synthetic strategies, 200 round turns per
session over 120 sessions, commission 0.10 a side:

    no skill                          net  -4,715 pts   observed -0.9423   p 0.4113
    slight skill, still loses to cost net  -3,275 pts   observed -0.8904   p 0.0005

The second **loses 3,275 points** and would clear any bar you care to set. There
is nothing wrong with the arithmetic: the null holds the trade count fixed, so a
high-turnover candidate's null is dominated by commission and sits far below zero,
and beating it means only *"your directional calls were better than coin flips"*.
That is a true statement and it is not *"this strategy makes money"*.

So a p-value from this source must never be read alone. The
`sortino > 0` floor in the pass rule is not a redundant sanity check -- **it is
the thing that stops this**, and it has to be applied wherever this null gates
anything, at L1 as well as at L2.

The conditional null does not have this shape as sharply, because re-running the
strategy on altered data lets it trade *differently* -- possibly less -- so its
null is not automatically commission-dominated.

**Its error rate has not been measured.** The FWER of 0.060, CI [0.041, 0.088], is
the conditional null with `max_of_n` at R = 150 (Task 38). It does not describe
this. A summary built on this source says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ...types import Blotter, BlotterRow
from ..metrics import SessionSpan

B_DEFAULT = 10_000


@dataclass(frozen=True, slots=True)
class RoundTurn:
    """One flat-to-flat excursion.

    `gross` is P&L before cost and `cost` is what the whole excursion paid, both
    sides. `session` is where the metric attributes it -- the session the
    *closing* fill happened in, which is what `metrics.session_returns` does and
    what this has to match to be the same statistic.
    """

    gross: float
    cost: float
    session: int
    n_fills: int


def round_turns(blotter: Blotter, calendar: Sequence[SessionSpan]) -> list[RoundTurn]:
    """Flat-to-flat excursions, in time order.

    A round turn ends when `position_after` returns to exactly zero. A flip
    through zero does not end one: `reconstruct` sets the position to the
    remaining exposure, which is non-zero, so the excursion continues -- and that
    is right, because a flip is one continuous piece of risk-taking.

    **A trailing excursion that never returns to flat is dropped, and the count
    is returned so it is visible.** Its P&L is unrealised and the metric never saw
    it either.
    """
    from ..metrics import _session_of

    out: list[RoundTurn] = []
    gross = cost = 0.0
    fills = 0
    open_ = False
    for row in sorted(blotter.rows, key=lambda r: (r.seq, r.order_id)):
        gross += row.realized_pnl
        cost += row.commission
        fills += 1
        open_ = True
        if row.position_after == 0:
            session = _session_of(row.ts, calendar)
            if session is not None:
                out.append(RoundTurn(gross=gross, cost=cost, session=session,
                                     n_fills=fills))
            gross = cost = 0.0
            fills = 0
            open_ = False
    return out


def unclosed_fills(blotter: Blotter) -> int:
    """Fills in a trailing excursion that never returned to flat. 0 is the norm."""
    n = 0
    for row in sorted(blotter.rows, key=lambda r: (r.seq, r.order_id)):
        n += 1
        if row.position_after == 0:
            n = 0
    return n


def null_sample(
    blotter: Blotter,
    calendar: Sequence[SessionSpan],
    *,
    b: int = B_DEFAULT,
    seed: int = 17,
    notional_base: float = 1.0,
    idle_charge: float | None = None,
) -> list[float]:
    """`b` Sortinos under the sign-flip null. This is `null_samples[i]`.

    Vectorised where it has to be -- a candidate with 100,000 round turns and
    b = 10,000 is 10^9 sign draws -- and deliberately *not* vectorised for the
    statistic itself: the loop calls `scorer.score_returns`, the same rule the
    gate is computed from, so the null and the observed score cannot drift apart.
    A re-implementation that was 3% faster and 1% different would be worthless.

    **It calls `score_returns` and not `metrics.sortino`, and the first version
    got that wrong.** `score_returns` is the whole rule, including the fallback
    from Sortino to Sharpe below `MIN_DOWNSIDE_SESSIONS` and the infinite-Sortino
    guard. Calling the bare statistic produced two defects that only appeared on
    real data, in campaign `nq-mvp-01`:

    * **The null measured a different statistic from the score.** Six of fifty
      candidates traded so little that the scorer reported
      `sharpe_per_session` -- including the campaign's best, at three trades --
      while their nulls were Sortino distributions. Comparing them is
      meaningless. Task 29 closed this at the FAR level, where every trial is
      charged `max(far_sortino, far_sharpe)` precisely so a strategy cannot
      choose which null judges it; that fix does not reach `max_of_n`, which
      compares raw scores, and this null inherited the gap.
    * **2.14% of draws were infinite.** A sign-flip in which every traded session
      comes out positive has zero downside deviation, and bare Sortino returns
      `+inf` there. Nine thousand infinities put `null_max_mean` at `inf` and
      inflated the p-value. `score_returns` routes those to the Sharpe leg
      instead, which is exactly what it exists for.
    """
    # The idle charge must reach the null identically, or the observed series
    # pays a cost the draws do not and the p-value moves (measured: 0.0367 ->
    # 0.1813). It is computed from the blotter here because `null_from_turns`
    # takes triples rather than a blotter, and passed through as a vector.
    from ..metrics import IDLE_CHARGE, idle_charges

    charge = IDLE_CHARGE if idle_charge is None else idle_charge
    return null_from_turns(round_turns(blotter, calendar),
                           [c.session for c in calendar],
                           b=b, seed=seed, notional_base=notional_base,
                           charges=idle_charges(blotter, calendar,
                                                idle_charge=charge,
                                                notional_base=notional_base))


def identity_primary(
    turns: Sequence[RoundTurn],
    sessions: Sequence[int],
    *,
    notional_base: float = 1.0,
    charges: Sequence[float] | None = None,
):
    """The **no-flip** draw: the one assignment that must reproduce the observed.

    A sign-flip null is exchangeable with the observed score only if the observed
    is *in the null's support*, and it is -- as the assignment that flips nothing.
    So this is a complete check on whether a stored score and a freshly-built null
    are talking about the same thing, and it costs one evaluation.

    **Built after the null and the observed silently diverged on real data.**
    `nq-orderflow-01`'s validation score was produced before the Amendment 3 idle
    charge reached that code path -- its run record has no `primary_uncharged`
    field, which is how you can tell -- and its validation null was rebuilt months
    later with the current scorer, which charges. The observed came out
    **-0.1516 on Sharpe** and every one of the four possible null draws came out
    around **-0.87 on Sortino**: a different objective *and* a different metric.
    The gap read as +17.8 standard deviations of skill and was an artefact.

    The class of bug matters more than the instance. `l2_gate.py` rebuilds nulls
    **now** for validation runs scored **then**, so any change to the metric
    between those two moments breaks comparability silently, and CLAUDE.md's
    "changing it now invalidates every calibration number in the repo" is exactly
    that hazard. A guard that re-derives the identity assignment catches every
    version skew, including the ones nobody thought of.

    Returns the `ScoreTuple`, so the caller can compare the metric as well as the
    number -- observed-on-Sharpe against a null of Sortinos is meaningless even
    when both are finite.
    """
    import numpy as np

    from ..scorer import score_returns

    slot = {s: i for i, s in enumerate(sessions)}
    acc = np.zeros(len(sessions), dtype=float)
    for t in turns:
        acc[slot[t.session]] += t.gross - t.cost
    if notional_base != 1.0:
        acc /= notional_base
    if charges is not None:
        acc += np.asarray(charges, dtype=float)
    return score_returns(acc.tolist())


def identity_from_blotter(
    blotter: Blotter,
    calendar: Sequence[SessionSpan],
    *,
    notional_base: float = 1.0,
    idle_charge: float | None = None,
):
    """`identity_primary`, built the way `null_sample` builds its own inputs.

    Kept beside `null_sample` and taking the same arguments on purpose: a guard
    that constructed its charge vector differently from the null it guards would
    be checking two things that were never the same.
    """
    from ..metrics import IDLE_CHARGE, idle_charges

    charge = IDLE_CHARGE if idle_charge is None else idle_charge
    return identity_primary(
        round_turns(blotter, calendar), [c.session for c in calendar],
        notional_base=notional_base,
        charges=idle_charges(blotter, calendar, idle_charge=charge,
                             notional_base=notional_base))


def null_from_turns(
    turns: Sequence[RoundTurn],
    sessions: Sequence[int],
    *,
    b: int = B_DEFAULT,
    seed: int = 17,
    notional_base: float = 1.0,
    charges: Sequence[float] | None = None,
) -> list[float]:
    """The same null, from round turns and a session list rather than a blotter.

    Split out so a measurement can carry `(gross, cost, session)` triples across a
    process boundary instead of whole blotters -- 24,000 candidates' blotters do
    not fit anywhere convenient, and the triples are all the null ever reads.
    `null_sample` is the ordinary entry point and delegates here.
    """
    import numpy as np

    from ..scorer import score_returns

    if not turns:
        # Nothing was traded, so there is no direction to destroy and no null to
        # build. An empty sample contributes nothing to `max_of_n`, which is what
        # should happen: a candidate that never traded cannot be the best of N.
        return []

    gross = np.array([t.gross for t in turns], dtype=float)
    cost = np.array([t.cost for t in turns], dtype=float)
    sess = np.array([t.session for t in turns], dtype=np.int64)

    # The metric is defined over EVERY session in the calendar, flat ones
    # included -- a strategy that sat out 300 of 384 sessions has 300 zeros in
    # its return vector and they are part of its Sortino. Building the full
    # vector rather than only the traded sessions is what keeps this the same
    # statistic.
    index = {s: i for i, s in enumerate(sessions)}
    slot = np.array([index[s] for s in sess], dtype=np.int64)
    n_sessions = len(sessions)

    charge_vec = None
    if charges is not None:
        if len(charges) != n_sessions:
            raise ValueError(
                f"charges has {len(charges)} entries for {n_sessions} sessions; "
                "the null must carry the same per-session charge as the observed "
                "series or the p-value is not comparing like with like")
        charge_vec = np.asarray(charges, dtype=float)

    rng = np.random.default_rng(seed)
    out: list[float] = []
    # Chunked: the full (b, n_turns) sign matrix would be gigabytes at realistic
    # trade counts.
    chunk = max(1, min(b, 4_000_000 // max(len(turns), 1)))
    done = 0
    while done < b:
        c = min(chunk, b - done)
        signs = rng.integers(0, 2, size=(c, len(turns)), dtype=np.int8) * 2 - 1
        contrib = signs * gross - cost
        acc = np.zeros((c, n_sessions), dtype=float)
        np.add.at(acc, (np.arange(c)[:, None], slot[None, :]), contrib)
        if notional_base != 1.0:
            acc /= notional_base
        if charge_vec is not None:
            # Already divided by notional_base by `idle_charges`, so it is added
            # after the division above rather than before it.
            acc += charge_vec
        for row in acc:
            out.append(score_returns(row.tolist()).primary)
        done += c
    return out
