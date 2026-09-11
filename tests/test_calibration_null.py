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
import json
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


# --------------------------------------------------------------------------- #
# one null per dataset, and the table says which
# --------------------------------------------------------------------------- #
NQ_NULL = REPO_ROOT / "data" / "calibration" / "empirical-null-nq-trades.json"


def test_every_committed_null_is_tagged_with_what_it_was_measured_on():
    """An untagged table is one nobody can check. Task 15 scored a real NQ run
    against the synthetic table and got a plausible number that meant nothing."""
    for path in (REPO_ROOT / "data" / "calibration").glob("empirical-null*.json"):
        if path.stem.endswith("-verification"):
            continue          # a report about a table, not a table
        doc = json.loads(path.read_text())
        assert doc.get("dataset"), f"{path.name} carries no dataset tag"


def test_asking_about_one_dataset_never_reads_another_ones_table():
    with pytest.raises(N.WrongNullTable):
        N.threshold_for(0.001, 86, dataset="nq-trades:NQ", path=N.NULL_PATH)
    with pytest.raises(N.WrongNullTable):
        N.threshold_for(0.001, 86, dataset="synthetic", path=NQ_NULL)


def test_a_dataset_with_no_measured_null_raises_rather_than_falling_back():
    """The failure that matters is silent reuse, so the absent case must be loud
    and must say what to run."""
    with pytest.raises(FileNotFoundError, match="tabulate_null"):
        N.threshold_for(0.001, 50, dataset="es-trades:ES")


def test_the_dataset_family_is_what_a_null_belongs_to():
    assert N.null_key("synthetic:s=0:seed=1") == "synthetic"
    assert N.null_key("nq-trades:NQ") == "nq-trades"
    assert N.null_path_for("synthetic:s=0.5:seed=9") == N.NULL_PATH
    assert N.null_path_for("nq-trades:NQ").name == "empirical-null-nq-trades.json"


def test_the_real_null_is_much_stricter_than_the_synthetic_one():
    """The number this task exists for. Synthetic sessions have far less
    dispersion than real ones, so the synthetic table sets thresholds a strategy
    with no edge can clear."""
    if not NQ_NULL.exists():
        pytest.skip("the real-data null has not been tabulated here")
    synthetic = N.threshold_for(0.001, 86, dataset="synthetic")
    real = N.threshold_for(0.001, 86, dataset="nq-trades:NQ")
    assert synthetic == pytest.approx(0.0336, abs=0.02)
    assert real > 0.4
    assert real > 5 * max(synthetic, 0.01)


def test_the_task_15_score_reads_as_nothing_against_the_right_null():
    """Sortino +0.0076 over 86 sessions, from the first real run. It was 0.18%
    against the synthetic table -- almost an accept at a 0.1% policy -- and is a
    coin flip against the real one."""
    if not NQ_NULL.exists():
        pytest.skip("the real-data null has not been tabulated here")
    score, n = 0.0076030169676602425, 86
    assert N.far_for(score, n, dataset="synthetic") < 0.01
    assert N.far_for(score, n, dataset="nq-trades:NQ") > 0.1


# --------------------------------------------------------------------------- #
# the block bootstrap
# --------------------------------------------------------------------------- #
def test_block_length_one_is_the_iid_resample_exactly():
    """Not approximately: the same generator state produces the same integers, so
    the old behaviour is a special case of the new code path rather than a
    parallel one. If this ever drifts, the block implementation is wrong."""
    import numpy as np

    pool = np.asarray(json.loads(N.NULL_PATH.read_text())["pool"], dtype=float)
    legacy = np.random.default_rng(99).choice(pool, size=(2000, 40), replace=True)
    blocked = pool[N._draw_indices(np.random.default_rng(99), pool.size, 40, 2000, 1)]
    assert np.array_equal(legacy, blocked)


def test_blocks_are_contiguous_and_wrap():
    import numpy as np

    idx = N._draw_indices(np.random.default_rng(3), 50, 12, 200, 8)
    steps = (idx[:, 1:] - idx[:, :-1]) % 50
    # every step is either +1 within a block or a jump to a new block start
    assert (steps == 1).mean() > 0.5, "blocks are not contiguous"
    assert idx.max() < 50 and idx.min() >= 0


def test_the_block_length_is_measured_from_the_pool():
    """A block length chosen for convenience is the same class of unexamined
    assumption this task exists to remove."""
    if not NQ_NULL.exists():
        pytest.skip("the real-data null has not been tabulated here")
    doc = json.loads(NQ_NULL.read_text())
    measured = doc["provenance"]["block_length_measurement"]
    assert doc["provenance"]["block_length_source"] == "measured"
    assert doc["block_length"] == measured["block_length"] > 1
    assert doc["bootstrap"] == "stationary"
    # the returns themselves are near-uncorrelated; their absolute values are not,
    # and that is what sets the block length
    assert abs(measured["lag1_returns"]) < measured["significance_band"]
    assert measured["lag1_abs_returns"] > measured["significance_band"]


def test_the_real_null_was_built_on_training_sessions_only():
    if not NQ_NULL.exists():
        pytest.skip("the real-data null has not been tabulated here")
    prov = json.loads(NQ_NULL.read_text())["provenance"]
    assert prov["split"] == "train"
    assert prov["sessions_in_pool"] < prov["sessions_scored"]
    assert prov["contaminated_sessions_remaining_in_pool"] == []
    for field in ("dataset_digest", "fill_model", "seed", "contract_selection",
                  "exclusion_rule", "blotter_digest"):
        assert prov.get(field) not in (None, ""), field


def test_tabulating_the_null_on_anything_but_train_is_refused():
    """Calibrating the gate with the data the gate is applied to is not
    recoverable afterwards, so it is refused at the call rather than reviewed."""
    from src.core.types import Split

    class Fake:
        name = "fake:X"

        def digest(self):
            return "0"

    for split in (Split.VALIDATION, Split.HOLDOUT):
        with pytest.raises(ValueError, match="training sessions only"):
            N.real_session_returns(Fake(), split=split)
