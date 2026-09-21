"""Property tests for the scoring metric.

The four invariances are the acceptance criterion for *any* metric that replaces
this one, not just for this one. They are written against a hand-built blotter so
they test the metric rather than the simulator.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.scoring import metrics  # noqa: E402
from src.core.scoring.metrics import MIN_SESSIONS, REJECT, SessionSpan  # noqa: E402
from src.core.scoring.scorer import score  # noqa: E402
from src.core.types import Blotter, BlotterRow, Side  # noqa: E402

DAY = 86_400 * 1_000_000_000


def calendar(n: int, start: int = 0) -> list[SessionSpan]:
    return [SessionSpan(i, start + i * DAY, start + i * DAY + DAY - 1) for i in range(n)]


def blotter_from(per_session: dict[int, list[float]], *, size: int = 2) -> Blotter:
    """One closing row per P&L figure, in the session it belongs to."""
    b = Blotter()
    seq = 0
    for session, pnls in sorted(per_session.items()):
        ts = session * DAY + 3600 * 1_000_000_000
        for pnl in pnls:
            seq += 1
            b.add(
                BlotterRow(
                    seq=seq,
                    ts=ts + seq,
                    order_id=seq,
                    side=Side.BUY if pnl >= 0 else Side.SELL,
                    price=100.0,
                    size=size,
                    position_after=0,
                    commission=0.02 * size,
                    realized_pnl=pnl,
                )
            )
    return b


def sample(
    n_sessions: int = 60, seed: int = 3, mu: float = 0.4
) -> tuple[Blotter, list[SessionSpan]]:
    import random

    rng = random.Random(seed)
    per = {}
    for s in range(n_sessions):
        k = rng.randint(0, 3)
        per[s] = [rng.gauss(mu, 2.0) for _ in range(k)]
    return blotter_from(per), calendar(n_sessions)


def scaled(b: Blotter, *, size_k: int = 1, price_k: float = 1.0,
           scale_commission: bool = True) -> Blotter:
    out = Blotter()
    for r in b.rows:
        out.add(
            BlotterRow(
                seq=r.seq,
                ts=r.ts,
                order_id=r.order_id,
                side=r.side,
                price=r.price * price_k,
                size=r.size * size_k,
                position_after=r.position_after,
                commission=r.commission * size_k * (price_k if scale_commission else 1.0),
                realized_pnl=r.realized_pnl * size_k * price_k,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# 1. trade-splitting invariance
# --------------------------------------------------------------------------- #
def test_splitting_every_trade_in_half_does_not_change_the_score():
    """Same positions, same returns, different bookkeeping."""
    b, cal = sample()
    split = Blotter()
    seq = 0
    for r in b.rows:
        assert r.size % 2 == 0
        for _ in range(2):
            seq += 1
            split.add(
                BlotterRow(
                    seq=seq, ts=r.ts, order_id=r.order_id, side=r.side, price=r.price,
                    size=r.size // 2, position_after=r.position_after,
                    commission=r.commission / 2, realized_pnl=r.realized_pnl / 2,
                )
            )
    a, c = score(b, period=cal), score(split, period=cal)
    assert a.primary == pytest.approx(c.primary)
    assert a.sharpe == pytest.approx(c.sharpe)
    assert a.net_pnl == pytest.approx(c.net_pnl)


# --------------------------------------------------------------------------- #
# 2. leverage invariance
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("k", [2, 5, 10])
def test_scaling_every_position_by_a_constant_does_not_change_the_score(k):
    """`scaled()` scales size and P&L but leaves `position_after` alone, so the
    idle charge cannot see the change. Measured with the charge off; the version
    that scales the position properly is
    `test_trading_bigger_does_not_dilute_the_idle_charge`."""
    b, cal = sample()
    a = score(b, period=cal, idle_charge=0.0)
    c = score(scaled(b, size_k=k), period=cal, idle_charge=0.0)
    assert a.primary == pytest.approx(c.primary)
    assert a.sharpe == pytest.approx(c.sharpe)
    assert c.net_pnl == pytest.approx(a.net_pnl * k)


# --------------------------------------------------------------------------- #
# 3. price-scale invariance -- the one the placeholder failed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("k", [0.5, 2.0, 100.0])
def test_scaling_the_price_process_does_not_change_the_score(k):
    """The ratio mechanics, with the idle charge off.

    The charge is a cost in absolute index points, so like the per-contract fee
    below it is a scale-breaking term -- see the next test, which asserts that
    deliberately. This one is about the ratio, so it is measured without it.
    """
    b, cal = sample()
    a = score(b, period=cal, idle_charge=0.0)
    c = score(scaled(b, price_k=k), period=cal, idle_charge=0.0)
    assert a.primary == pytest.approx(c.primary)
    assert a.sharpe == pytest.approx(c.sharpe)


@pytest.mark.parametrize("k", [2.0, 100.0])
def test_the_idle_charge_is_a_scale_breaking_term_and_the_test_says_so(k):
    """Same exemption as the per-contract fee, asserted rather than discovered.

    The charge is a fixed number of index points, so against a price process a
    hundred times larger it is a hundredth of the relative cost and the score
    rises toward the uncharged value. That is correct for a cost expressed in
    points and wrong to leave implicit, because the same arithmetic against
    POSITION size would be a reward hack -- trade bigger, dilute the charge --
    which is why `position_scale` exists and why the next test exists.
    """
    b, cal = sample()
    small = score(b, period=cal).primary
    large = score(scaled(b, price_k=k), period=cal).primary
    assert small != pytest.approx(large)
    free = score(scaled(b, price_k=k), period=cal, idle_charge=0.0).primary
    assert abs(large - free) < abs(small - score(b, period=cal, idle_charge=0.0).primary)


def test_trading_bigger_does_not_dilute_the_idle_charge():
    """The reward hack `position_scale` closes.

    P&L scales with contracts and a flat charge does not, so without scaling the
    charge by position a strategy escapes it by sizing up -- measured, at 100
    contracts a 0.18 charge moved a score from -0.3554 to -0.0676, which is
    within a rounding error of having no charge at all. Charging per contract
    restores the invariance exactly: double the position and P&L, margin and the
    charge all double.
    """
    cal = calendar(40)

    def run(contracts):
        """Open and close a position in each of the first 20 sessions, so
        `position_after` is a real sequence rather than the closing-rows-only
        shape `blotter_from` produces."""
        import random
        rng = random.Random(9)
        b, seq = Blotter(), 0
        for sess in range(20):
            base = sess * DAY + 3600 * 1_000_000_000
            seq += 1
            b.add(BlotterRow(seq=seq, ts=base, order_id=seq, side=Side.BUY,
                             price=100.0, size=contracts,
                             position_after=contracts, commission=0.02 * contracts,
                             realized_pnl=0.0))
            seq += 1
            b.add(BlotterRow(seq=seq, ts=base + 1000, order_id=seq, side=Side.SELL,
                             price=100.0, size=contracts, position_after=0,
                             commission=0.02 * contracts,
                             realized_pnl=rng.gauss(0.3, 2.0) * contracts))
        return b

    one, two = run(1), run(2)
    assert max(abs(r.position_after) for r in two.rows) == 2, "fixture is wrong"
    assert score(one, period=cal).primary == pytest.approx(
        score(two, period=cal).primary), (
        "doubling every position changed the score; the idle charge is not "
        "scaling with contracts and can be diluted by trading bigger")


def test_a_per_contract_fee_is_a_scale_breaking_term_and_the_test_says_so():
    """Price-scale invariance holds for the *ratio*, but only if costs are
    expressed in the same units as the price. A fixed per-contract fee is not: at
    a hundredth of the price scale it is a hundred times the cost, and the score
    is right to fall. Asserted so the exemption is deliberate rather than a
    surprise in the first real adapter.
    """
    b, cal = sample()
    fixed_fee = score(scaled(b, price_k=100.0, scale_commission=False), period=cal)
    scaled_fee = score(scaled(b, price_k=100.0, scale_commission=True), period=cal)
    assert fixed_fee.primary != pytest.approx(scaled_fee.primary)
    assert fixed_fee.primary > scaled_fee.primary  # the fee shrank in relative terms


# --------------------------------------------------------------------------- #
# 4. session padding -- a chosen property, not an accident
# --------------------------------------------------------------------------- #
def test_padding_with_flat_sessions_lowers_the_score():
    # a profitable sample, so "lowers" has an unambiguous direction: padding a
    # losing strategy moves its ratio *up* toward zero, which is the same effect
    b, cal = sample(n_sessions=60, mu=1.5)
    base = score(b, period=cal)
    assert base.primary > 0, "need a profitable sample for the direction to mean anything"
    for extra in (10, 60, 200):
        padded = score(b, period=calendar(60 + extra))
        assert padded.primary < base.primary, (
            f"adding {extra} flat sessions did not lower the score"
        )
    # and the diagnostic keeps selectivity visible
    from src.core.scoring.scorer import score_diagnostics

    d = score_diagnostics(b, calendar(120))
    assert d.n_sessions == 120 and d.sessions_with_exposure < 120


def test_a_strategy_that_trades_three_times_and_wins_is_not_extraordinary():
    """The reason flat sessions count: conditioning on days-it-traded is what
    makes three lucky trades look like an edge."""
    b = blotter_from({0: [5.0], 1: [5.0], 2: [5.0]})
    conditioned = score(b, period=calendar(3))
    full = score(b, period=calendar(60))
    assert conditioned.rejected, "three sessions should not clear the floor"
    assert not full.rejected
    assert full.primary != REJECT


# --------------------------------------------------------------------------- #
# the observation floor
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("n", [1, 5, 29])
def test_below_the_floor_the_score_is_the_reject_sentinel(n):
    b, _ = sample(n_sessions=n)
    s = score(b, period=calendar(n))
    assert s.rejected and s.primary == REJECT
    assert str(MIN_SESSIONS) in s.reject_reason
    assert s.significance == REJECT


def test_at_the_floor_the_score_is_a_measurement():
    b, _ = sample(n_sessions=MIN_SESSIONS)
    s = score(b, period=calendar(MIN_SESSIONS))
    assert not s.rejected and s.primary != REJECT


def test_no_calendar_is_a_reject_not_a_fallback_to_per_trade():
    b, _ = sample()
    s = score(b, period=None)
    assert s.rejected and s.primary == REJECT


# --------------------------------------------------------------------------- #
# the downside-deviation convention
# --------------------------------------------------------------------------- #
def test_downside_deviation_divides_by_all_observations():
    """Dividing by the count of losing observations instead makes the denominator
    shrink as a strategy improves, so Sortino explodes on a handful of losses --
    the classic pathology, and another non-monotone curve."""
    returns = [1.0] * 99 + [-1.0]
    dd = metrics.downside_deviation(returns)
    assert dd == pytest.approx(math.sqrt(1.0 / 100))
    pathological = math.sqrt(1.0 / 1)  # divided by the one losing observation
    assert dd < pathological
    assert metrics.sortino(returns) == pytest.approx(0.98 / dd)


def test_mar_is_zero_and_stated():
    assert metrics.MAR == 0.0
    assert metrics.sortino([0.0] * 50) == 0.0


def test_no_losing_session_is_flagged_rather_than_silently_infinite():
    """The raw statistic is infinite; `score` no longer returns it.

    Task 6 added the downside floor: too few below-MAR sessions falls back to
    Sharpe, and a series with no dispersion at all is rejected rather than
    accepted. See tests/test_calibration_null.py for the guard's own tests.
    """
    assert metrics.sortino([1.0] * 50) == float("inf")
    s = score(blotter_from({i: [1.0] for i in range(40)}), period=calendar(40))
    assert s.rejected and s.primary == REJECT
    assert "no dispersion" in s.reject_reason


# --------------------------------------------------------------------------- #
# sharpe rides along for the committee
# --------------------------------------------------------------------------- #
def test_sharpe_is_computed_on_the_same_series():
    b, cal = sample()
    s = score(b, period=cal)
    returns = metrics.session_returns(b, cal)
    assert s.sharpe == pytest.approx(metrics.sharpe(returns))
    assert s.skew == pytest.approx(metrics.skewness(returns))
    assert s.kurtosis == pytest.approx(metrics.excess_kurtosis(returns))
    assert s.n_sessions == len(returns)


# --------------------------------------------------------------------------- #
# diagnostics are recorded, never scored
# --------------------------------------------------------------------------- #
def test_diagnostics_do_not_move_the_score():
    from src.core.scoring.scorer import score_diagnostics

    b, cal = sample()
    base = score(b, period=cal)
    d = score_diagnostics(b, cal)
    assert d.max_drawdown <= 0
    assert 0.0 <= d.profit_concentration_top5pct <= 1.0
    assert d.by_month and d.by_weekday and d.by_hour_utc
    assert "descriptive, not inferential" in d.seasonality_caveat
    # scoring again after computing diagnostics changes nothing
    assert score(b, period=cal).primary == pytest.approx(base.primary)


def test_the_placeholder_metric_is_still_callable():
    """Kept so the broken curve stays reproducible rather than merely archived."""
    b, _ = sample()
    t, n, net = metrics.placeholder_score(b)
    assert n > 0 and isinstance(t, float)


def test_strategies_cannot_import_the_metric():
    import subprocess

    probe = REPO_ROOT / "src" / "strategies" / "_metric_probe_tmp.py"
    probe.write_text("from src.core.scoring import metrics  # noqa: F401\n")
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import src.strategies._metric_probe_tmp"],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert r.returncode != 0
        assert "may not import src.core.scoring.metrics" in r.stderr
    finally:
        probe.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# the screening criterion, isolated from any strategy
# --------------------------------------------------------------------------- #
def _series(mean: float, sd: float, n: int = 200, seed: int = 0) -> list[float]:
    import random

    rng = random.Random(seed)
    return [rng.gauss(mean, sd) for _ in range(n)]


def test_sortino_is_monotone_in_edge_when_the_risk_profile_is_held_fixed():
    """The metric's own screen, with no strategy in the way.

    The end-to-end sweep turns over at the strongest planted signal. This test
    establishes that the turnover is not the metric: hold dispersion constant and
    grow the edge, and Sortino rises monotonically.
    """
    curve = [metrics.sortino(_series(m, 1.0)) for m in (0.0, 0.1, 0.25, 0.5, 1.0)]
    assert curve == sorted(curve), curve


def test_sortino_correctly_falls_when_the_loss_tail_grows_faster_than_the_edge():
    """And the other half: a payoff whose upside is capped and whose loss tail
    widens *should* score worse as the tail widens, even while its mean return
    rises. That is the regime the end-to-end sweep enters at high planted signal
    -- the strategy's bracket caps every win at 0.74 while a stop gapped through
    under the pessimistic fill model loses more and more -- and a ratio that did
    not fall there would be the broken one.

    Numbers are stylised after the measured regime: mean win pinned at 0.74,
    win rate 0.91 -> 0.96, mean loss 3.7 -> 12.0, trades per session 21 -> 30.
    """
    import random

    def sessions(p_win: float, loss: float, trades: int, n: int = 500, seed: int = 1):
        rng = random.Random(seed)
        return [
            sum(0.74 if rng.random() < p_win else -loss for _ in range(trades))
            for _ in range(n)
        ]

    weak_tail = sessions(0.91, 3.7, 21)
    wide_tail = sessions(0.96, 12.0, 30)
    assert sum(wide_tail) / len(wide_tail) > 0, "the wide-tail payoff still makes money"
    assert metrics.sortino(wide_tail) < metrics.sortino(weak_tail), (
        "a widening loss tail did not lower the ratio"
    )
    # and the mechanism: dispersion grows faster than the edge
    import statistics as st

    assert st.pstdev(wide_tail) > 2 * st.pstdev(weak_tail)


def test_the_floor_is_what_stops_a_tiny_sample_looking_extraordinary():
    """Answers 'did Sortino need the floor, and at what N'.

    A handful of sessions with no loss in them gives an infinite ratio. The floor
    is the only thing standing between that and an accepted trial.
    """
    lucky = blotter_from({i: [3.0] for i in range(5)})
    assert score(lucky, period=calendar(5)).rejected
    assert metrics.sortino(metrics.session_returns(lucky, calendar(5))) == float("inf")
