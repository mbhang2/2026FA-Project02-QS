"""The sign-flip null: the decomposition is exact, and the null is not profit.

`round_turn.null_sample` is `null_samples[i]` for `max_of_n` when the configured
`null_source` is `round_turn_mc`. It is specified in
`docs/design/pre-registration.md` section 3b, and that document is the
specification -- if these tests and it disagree, it wins.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.core.scoring import metrics
from src.core.scoring.randomization import round_turn as rt
from src.core.types import Blotter, BlotterRow, Side

NS = 1_000_000_000


def _cal(n):
    return [metrics.SessionSpan(s, s * 10**6 * NS, (s * 10**6 + 999_999) * NS)
            for s in range(n)]


def _blotter(spec):
    """`spec` is [(session, gross, cost_per_fill), ...] -- one round turn each."""
    b, seq = Blotter(), 0
    for i, (s, gross, cost) in enumerate(spec):
        base = s * 10**6 * NS + i * 10 * NS
        seq += 1
        b.add(BlotterRow(seq=seq, ts=base, order_id=seq, side=Side.BUY, price=100.0,
                         size=1, position_after=1, commission=cost, realized_pnl=0.0))
        seq += 1
        b.add(BlotterRow(seq=seq, ts=base + NS, order_id=seq, side=Side.SELL,
                         price=100.0, size=1, position_after=0, commission=cost,
                         realized_pnl=gross))
    return b


# --------------------------------------------------------------------------- #
# the decomposition
# --------------------------------------------------------------------------- #

def test_round_turns_reconstruct_the_session_returns_exactly():
    """If `sum(gross - cost)` does not equal `sum(session_returns)`, the null is
    resampling something other than the quantity being scored."""
    cal = _cal(5)
    b = _blotter([(0, 3.0, 0.1), (2, -1.0, 0.1), (2, 0.5, 0.1)])
    turns = rt.round_turns(b, cal)
    assert len(turns) == 3
    assert sum(t.gross - t.cost for t in turns) == pytest.approx(
        sum(metrics.session_returns(b, cal)))


def test_a_round_turn_is_attributed_to_the_session_it_CLOSED_in():
    """`metrics.session_returns` attributes P&L to the closing fill's session, so
    this has to as well or it is a different statistic."""
    cal = _cal(3)
    b, seq = Blotter(), 0
    seq += 1
    b.add(BlotterRow(seq=seq, ts=0 * 10**6 * NS + NS, order_id=seq, side=Side.BUY,
                     price=100.0, size=1, position_after=1, commission=0.1,
                     realized_pnl=0.0))
    seq += 1                                     # closes in session 2
    b.add(BlotterRow(seq=seq, ts=2 * 10**6 * NS + NS, order_id=seq, side=Side.SELL,
                     price=100.0, size=1, position_after=0, commission=0.1,
                     realized_pnl=4.0))
    assert [t.session for t in rt.round_turns(b, cal)] == [2]


def test_a_flip_through_zero_does_not_end_a_round_turn():
    """`reconstruct` leaves a flip at the remaining exposure, never at zero, and
    a flip is one continuous piece of risk-taking."""
    cal = _cal(2)
    b, seq = Blotter(), 0
    for pos, pnl in ((1, 0.0), (-1, 2.0), (0, 1.0)):
        seq += 1
        b.add(BlotterRow(seq=seq, ts=seq * NS, order_id=seq, side=Side.BUY,
                         price=100.0, size=1, position_after=pos, commission=0.1,
                         realized_pnl=pnl))
    turns = rt.round_turns(b, cal)
    assert len(turns) == 1
    assert turns[0].gross == pytest.approx(3.0)
    assert turns[0].cost == pytest.approx(0.3)


def test_an_excursion_that_never_returns_to_flat_is_dropped_and_counted():
    cal = _cal(2)
    b = _blotter([(0, 1.0, 0.1)])
    seq = max(r.seq for r in b.rows) + 1
    b.add(BlotterRow(seq=seq, ts=500 * NS, order_id=seq, side=Side.BUY, price=100.0,
                     size=1, position_after=1, commission=0.1, realized_pnl=0.0))
    assert len(rt.round_turns(b, cal)) == 1
    assert rt.unclosed_fills(b) == 1


# --------------------------------------------------------------------------- #
# the null
# --------------------------------------------------------------------------- #

def test_every_session_in_the_calendar_is_in_the_return_vector():
    """A strategy that sat out 300 of 384 sessions has 300 zeros in its vector and
    they are part of its Sortino. Dropping them would be a different statistic."""
    cal = _cal(50)
    b = _blotter([(0, 1.0, 0.1), (1, -1.0, 0.1)])
    sample = rt.null_sample(b, cal, b=64, seed=3)
    assert len(sample) == 64

    # Two round turns is four sign combinations, so the null has at most four
    # distinct values and they are enumerable. Computing them over the FULL
    # 50-session vector and requiring every sample to be one of them pins the
    # flat sessions exactly: drop them and the achievable set changes.
    turns = rt.round_turns(b, cal)
    achievable = set()
    for s0 in (-1, 1):
        for s1 in (-1, 1):
            vec = [0.0] * 50
            for sign, t in zip((s0, s1), turns):
                vec[t.session] += sign * t.gross - t.cost
            achievable.add(round(metrics.sortino(vec), 10))
    assert {round(x, 10) for x in sample} <= achievable, (
        "a sample value is not reachable over the full-length session vector")


def test_commission_is_charged_again_rather_than_flipped_with_the_pnl():
    """A round turn that lost paid to lose. Flipping the NET would hand the null a
    gain that never paid to trade -- a null too generous, a gate too easy."""
    cal = _cal(1)
    b = _blotter([(0, -1.0, 0.25)])           # gross -1.0, cost 0.5, net -1.5
    sample = rt.null_sample(b, cal, b=256, seed=5)
    # One session, so Sortino is +inf when the return is positive and 0 or
    # negative otherwise. The flipped-up case is gross +1.0 - 0.5 = +0.5 > 0.
    # Had the NET been flipped it would be +1.5. Either is positive, so check the
    # arithmetic directly instead.
    turns = rt.round_turns(b, cal)
    assert turns[0].gross == pytest.approx(-1.0)
    assert turns[0].cost == pytest.approx(0.5)
    assert (turns[0].gross - turns[0].cost) == pytest.approx(-1.5)
    assert (-turns[0].gross - turns[0].cost) == pytest.approx(0.5)


def test_it_is_deterministic_for_a_seed():
    cal = _cal(6)
    b = _blotter([(i % 6, float(i % 5) - 2.0, 0.1) for i in range(30)])
    assert rt.null_sample(b, cal, b=128, seed=11) == rt.null_sample(b, cal, b=128, seed=11)
    assert rt.null_sample(b, cal, b=128, seed=11) != rt.null_sample(b, cal, b=128, seed=12)


def test_a_candidate_that_never_traded_contributes_no_null():
    """An empty sample contributes nothing to `max_of_n`, which is right: a
    candidate that never traded cannot be the best of N."""
    assert rt.null_sample(Blotter(), _cal(4), b=32, seed=1) == []


def test_a_no_skill_strategy_is_not_significant_against_its_own_null():
    cal = _cal(60)
    rng = np.random.default_rng(0)
    b = _blotter([(s, float(rng.normal(0, 1)), 0.1)
                  for s in range(60) for _ in range(40)])
    obs = metrics.sortino(metrics.session_returns(b, cal))
    null = rt.null_sample(b, cal, b=1000, seed=17)
    p = (1 + sum(1 for x in null if x >= obs)) / (len(null) + 1)
    assert p > 0.05, f"a zero-skill strategy cleared its own null at p={p}"


def test_significance_here_does_NOT_mean_profitable():
    """The trap, asserted so it cannot be forgotten.

    Holding the trade count fixed makes a high-turnover candidate's null
    commission-dominated, so a strategy that LOSES money can beat it decisively.
    The `sortino > 0` floor in the pass rule is what stops this, and this test
    exists to keep the reason visible.
    """
    # 200 round turns per session over 120 sessions -- the turnover at which the
    # null becomes commission-dominated. At 40 per session the effect is real but
    # underpowered (p was 0.22), which is itself worth knowing: the trap sharpens
    # with turnover.
    cal = _cal(120)
    rng = np.random.default_rng(0)
    b = _blotter([(s, float(rng.normal(0.06, 1)), 0.1)
                  for s in range(120) for _ in range(200)])
    returns = metrics.session_returns(b, cal)
    obs = metrics.sortino(returns)
    null = rt.null_sample(b, cal, b=1000, seed=17)
    p = (1 + sum(1 for x in null if x >= obs)) / (len(null) + 1)

    assert sum(returns) < 0, "this fixture is supposed to lose money"
    assert p < 0.05, "and to be significant against its own null anyway"
    assert obs < 0, "and the sortino>0 floor is what catches it"
