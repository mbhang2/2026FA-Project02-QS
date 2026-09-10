"""The empirical null, the reference strategy and the downside guard.

The point of this file is that the numbers an accept rule will be built on are
measured rather than assumed. The acceptance test is the last one: at the
threshold the table says gives a 0.1% false-acceptance rate, fresh noise is
accepted about 0.1% of the time.
"""

from __future__ import annotations

import math
import statistics as st
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.scoring import metrics  # noqa: E402
from src.core.scoring.calibration import ReferenceStrategy  # noqa: E402
from src.core.scoring.calibration import null as N  # noqa: E402
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.scoring.metrics import MIN_DOWNSIDE_SESSIONS, SessionSpan  # noqa: E402
from src.core.scoring.scorer import score  # noqa: E402
from src.core.types import Blotter, BlotterRow, Side, Split  # noqa: E402

DAY = 86_400 * 1_000_000_000


def calendar(n: int) -> list[SessionSpan]:
    return [SessionSpan(i, i * DAY, i * DAY + DAY - 1) for i in range(n)]


def blotter(pnls: list[float]) -> Blotter:
    b = Blotter()
    for i, p in enumerate(pnls):
        b.add(BlotterRow(seq=i, ts=i * DAY + 3600 * 10**9, order_id=i, side=Side.BUY,
                         price=100.0, size=1, position_after=0, commission=0.0,
                         realized_pnl=p))
    return b


# --------------------------------------------------------------------------- #
# the rig is apparatus, not strategy
# --------------------------------------------------------------------------- #
def test_strategies_cannot_import_the_calibration_rig():
    """Same reason as the scorer: a strategy that can read the null it is judged
    against can be written to clear it."""
    probe = REPO_ROOT / "src" / "strategies" / "_probe_calibration.py"
    probe.write_text("from src.core.scoring import calibration  # noqa: F401\n")
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import src.strategies._probe_calibration"],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert r.returncode != 0
        assert "may not import src.core.scoring.calibration" in r.stderr
    finally:
        probe.unlink(missing_ok=True)


def test_the_reference_strategy_payoff_shape_is_invariant_to_signal_strength():
    """The criterion Task 4 stated wrongly. "Acceptance must be monotone in
    signal strength" is a property of the metric only if the reference
    strategy's payoff shape is constant; a fixed bracket guaranteed it was not.
    """
    ratios = {}
    for sig in (0.0, 0.5, 1.0):
        wins, losses = [], []
        for seed in (1, 2):
            eng = Engine(
                SyntheticDataset(SyntheticSpec(n_sessions=175, ticks_per_session=390,
                                               vol_per_tick=0.15, signal_strength=sig,
                                               seed=seed)),
                ReferenceStrategy(), split=Split.VALIDATION, warmup_sessions=1,
            )
            eng.run()
            closes = [r.realized_pnl - r.commission for r in eng.blotter.rows
                      if r.realized_pnl != 0.0]
            wins += [c for c in closes if c > 0]
            losses += [c for c in closes if c <= 0]
        ratios[sig] = st.mean(wins) / abs(st.mean(losses))

    lo, hi = min(ratios.values()), max(ratios.values())
    assert hi / lo < 1.3, (
        f"the payoff shape still moves with signal strength: {ratios}. "
        "Monotonicity of acceptance is then not a property of the metric."
    )


def test_the_reference_strategy_is_flat_at_every_session_boundary():
    """Holding across the synthetic generator's session reset measures the
    generator, not the metric."""
    eng = Engine(
        SyntheticDataset(SyntheticSpec(n_sessions=175, ticks_per_session=390,
                                       vol_per_tick=0.15, signal_strength=1.0, seed=1)),
        ReferenceStrategy(), split=Split.VALIDATION, warmup_sessions=1,
    )
    rec = eng.run()
    held = {s: p for s, p in rec.end_of_session_positions.items() if p != 0}
    assert not held, f"the reference strategy carried a position overnight: {held}"


# --------------------------------------------------------------------------- #
# the downside guard
# --------------------------------------------------------------------------- #
def test_too_few_losing_sessions_falls_back_to_sharpe():
    n = 40
    few = score(blotter([1.0 + 0.01 * i for i in range(36)] + [-1.0] * 4),
                period=calendar(n))
    assert few.metric == "sharpe_per_session"
    assert str(MIN_DOWNSIDE_SESSIONS) in few.reject_reason
    assert math.isfinite(few.primary)

    enough = score(blotter([1.0 + 0.01 * i for i in range(35)] + [-1.0] * 5),
                   period=calendar(n))
    assert enough.metric == "sortino_per_session"


def test_no_losing_session_at_all_is_bounded_rather_than_infinite():
    """An unbounded auto-accept does not just pass one bad candidate; it steers
    the search toward whatever produces it."""
    s = score(blotter([1.0 + 0.01 * i for i in range(40)]), period=calendar(40))
    assert s.metric == "sharpe_per_session"
    assert math.isfinite(s.primary) and s.primary > 0


def test_a_dispersion_free_blotter_is_rejected_not_accepted():
    s = score(blotter([1.0] * 40), period=calendar(40))
    assert s.rejected and s.primary == metrics.REJECT
    assert "no dispersion" in s.reject_reason


def test_the_fallback_path_has_its_own_tabulated_null():
    doc = N.load()
    assert "sharpe_per_session" in doc["metrics"]
    t = N.threshold_for(0.001, 30, metric="sharpe_per_session")
    assert math.isfinite(t)
    # and it is not silently the same table as Sortino's
    assert t != N.threshold_for(0.001, 30, metric="sortino_per_session")


# --------------------------------------------------------------------------- #
# the tabulated null
# --------------------------------------------------------------------------- #
def test_the_committed_null_records_how_it_was_made():
    doc = N.load()
    for field in ("bootstrap_trials_per_n", "bootstrap_seed", "pool_size",
                  "far_grid", "n_grid", "provenance", "pool"):
        assert field in doc, f"the tabulated null does not record {field}"
    assert doc["provenance"]["strategy"].endswith("ReferenceStrategy")
    assert doc["pool_size"] == len(doc["pool"])
    assert doc["bootstrap_trials_per_n"] >= 10_000


def test_the_vectorised_statistics_match_the_scalar_definitions():
    """The bootstrap is only meaningful if it computes the same statistic the
    scorer does."""
    rng = np.random.default_rng(0)
    sample = rng.normal(0.1, 1.0, size=(200, 37))
    fast_sortino = N._sortino(sample)
    fast_sharpe = N._sharpe(sample)
    for i in range(0, 200, 17):
        row = list(sample[i])
        assert fast_sortino[i] == pytest.approx(metrics.sortino(row))
        assert fast_sharpe[i] == pytest.approx(metrics.sharpe(row))
        assert N._downside_count(sample)[i] == metrics.downside_count(row)


def test_thresholds_fall_as_sessions_accumulate():
    """More observations, less score needed for the same false-acceptance rate."""
    for far in (0.05, 0.01, 0.001):
        curve = [N.threshold_for(far, n) for n in (30, 50, 100, 200)]
        assert curve == sorted(curve, reverse=True), (far, curve)


def test_thresholds_rise_as_the_false_acceptance_rate_tightens():
    for n in (30, 50, 100):
        curve = [N.threshold_for(f, n) for f in (0.05, 0.01, 0.001)]
        assert curve == sorted(curve), (n, curve)


def test_far_for_inverts_threshold_for():
    for n in (30, 50, 100):
        for far in (0.05, 0.01, 0.002):
            t = N.threshold_for(far, n)
            assert N.far_for(t, n) == pytest.approx(far, rel=0.15)


def test_asking_outside_the_tabulated_tail_raises_rather_than_extrapolating():
    with pytest.raises(ValueError, match="outside the tabulated range"):
        N.threshold_for(1e-9, 30)


def test_expected_max_grows_with_the_number_of_trials():
    """What DSR deflates by. The closed form exists only for Sharpe; with an
    empirical null it is a simulation, for any metric."""
    curve = [N.expected_max(k, 30) for k in (1, 10, 100, 1000)]
    assert curve == sorted(curve)


# --------------------------------------------------------------------------- #
# the acceptance test for the whole deliverable
# --------------------------------------------------------------------------- #
def test_the_null_holds_out_of_sample():
    """The acceptance test for the deliverable, held out rather than in-sample.

    The committed pool is split by run: the quantiles are re-tabulated from one
    half and the false-acceptance rate is measured on bootstrap draws from the
    other. Splitting the same pool rather than simulating a small fresh one keeps
    the two halves the same size -- the bootstrap's upper tail depends on how
    many extreme sessions the pool contains, so a smaller verification pool
    measures a thinner tail and flatters the result.
    """
    doc = N.load()
    pool = np.asarray(doc["pool"], dtype=float)
    half = len(pool) // 2
    a, b = pool[:half], pool[half:]

    table = N.tabulate(a, trials=40_000, n_grid=(30, 50), seed=99)
    rng = np.random.default_rng(31337)
    for n in (30, 50):
        draws = rng.choice(b, size=(120_000, n), replace=True)
        scores = N._sortino(draws)
        for far in (0.05, 0.01, 0.001):
            t = table["table"]["sortino_per_session"][str(n)]["quantiles"][str(far)]
            measured = float((scores >= t).mean())
            assert measured == pytest.approx(far, rel=0.6), (
                f"N={n}: threshold from one half of the pool promises far={far}, "
                f"the held-out half measured {measured:.5f}"
            )


@pytest.mark.slow
def test_false_acceptance_on_freshly_simulated_noise_is_not_worse_than_promised():
    """End-to-end on data simulated from seeds the table never saw.

    One-sided: a small fresh pool under-represents the tail and so measures a
    rate below target, which is the safe direction. The two-sided check is
    `test_the_null_holds_out_of_sample`, where both halves are the same size.
    The rule this replaces fired on 6% of pure noise while claiming 0.1%.
    """
    fresh = np.asarray(
        N.null_session_returns(runs=40, n_sessions=175, ticks_per_session=390,
                               seed0=990_000)
    )
    rng = np.random.default_rng(4242)
    for n, far in ((30, 0.001), (30, 0.01), (50, 0.001)):
        draws = rng.choice(fresh, size=(100_000, n), replace=True)
        measured = float((N._sortino(draws) >= N.threshold_for(far, n)).mean())
        assert measured <= far * 2.0, (
            f"at N={n} the table promises far={far} and fresh noise measured "
            f"{measured:.5f}"
        )
