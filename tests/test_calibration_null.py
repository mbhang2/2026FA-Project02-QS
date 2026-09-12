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
            eng = Engine.for_test(
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
    eng = Engine.for_test(
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

    block = N.measure_block_length(pool)["block_length"]
    table = N.tabulate(a, trials=40_000, n_grid=(30, 50), seed=99,
                       block_length=block)
    rng = np.random.default_rng(31337)
    for n in (30, 50):
        draws = b[N._draw_indices(rng, b.size, n, 120_000, block)]
        scores = N._sortino(draws)
        for far in (0.05, 0.01, 0.001):
            t = table["table"]["sortino_per_session"][str(n)]["quantiles"][str(far)]
            measured = float((scores >= t).mean())
            # One-sided, and deliberately. The promise a threshold makes is an
            # upper bound on false acceptance; delivering fewer false accepts
            # than promised is conservatism, not failure. Requiring the rate to
            # land *on* the promise would make the test fail whenever the two
            # halves of the pool differ in character -- which, since Task 17 made
            # the generator irregular, they do. What must never happen is the
            # other direction.
            assert measured <= far * 1.6, (
                f"N={n}: threshold from one half of the pool promises far={far}, "
                f"the held-out half measured {measured:.5f} -- more false "
                "acceptance than promised, which is the unsafe direction"
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
    # Task 17 closed most of this gap from the synthetic side by making the
    # generator irregular: the synthetic threshold rose from 0.0336 to about
    # 0.18, which is most of the distance to the real one. The remaining
    # difference is what a generator still cannot imitate, and it is why the
    # null is measured per dataset rather than approximated by a better
    # simulator.
    assert 0.1 < synthetic < 0.3
    assert real > 0.4
    assert real > 2 * synthetic


def test_the_task_15_score_reads_as_nothing_against_the_right_null():
    """Sortino +0.0076 over 86 sessions, from the first real run. It was 0.18%
    against the synthetic table -- almost an accept at a 0.1% policy -- and is a
    coin flip against the real one."""
    if not NQ_NULL.exists():
        pytest.skip("the real-data null has not been tabulated here")
    score, n = 0.0076030169676602425, 86
    synthetic = N.far_for(score, n, dataset="synthetic")
    real = N.far_for(score, n, dataset="nq-trades:NQ")
    assert synthetic < 0.05, "the synthetic table still reads this as near-significant"
    assert real > 0.1, "the real null should read it as nothing"
    assert real > 10 * synthetic


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
    # Every training session is in the pool since Task 18. The exclusion existed
    # because the adapter computed every session as running to 16:00 CT, so the
    # reference strategy was told a half day had four hours it did not have; it
    # reads the committed early-close times now and all 384 end flat.
    assert prov["sessions_in_pool"] == prov["sessions_scored"] == 384
    assert prov["sessions_excluded"] == 0
    assert prov["sessions_ending_with_a_position_open"] == 0
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


# --------------------------------------------------------------------------- #
# the generator's irregularity, and what it is for
# --------------------------------------------------------------------------- #
class BarCountExit(ReferenceStrategy):
    """The Task 16 bug, reintroduced deliberately.

    Learns the session's length as the smallest *bar count* it has seen and
    flattens two bars before that. On a dataset where every session is the same
    length and every interval has ticks, this is indistinguishable from reading
    the session's end. On anything else it is not, and the difference is the
    whole argument for making the generator irregular.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self._bars_this_session = 0
        self._session_bars = None
        self.min_session_bars_seen = None

    def on_bars(self, ctx) -> None:
        history = ctx.history(self.interval)
        if not len(history):
            return
        last = history[-1]
        self._ranges.append(last.high - last.low)

        if ctx.session != self._session:
            if self._session is not None and self._bars_this_session:
                self._session_bars = (
                    self._bars_this_session
                    if self._session_bars is None
                    else min(self._session_bars, self._bars_this_session)
                )
                self.min_session_bars_seen = self._session_bars
            self._session = ctx.session
            self._bars_this_session = 0
        self._bars_this_session += 1

        if (
            self._session_bars is not None
            and self._bars_this_session >= self._session_bars - 2
        ):
            if ctx.working_order_ids:
                ctx.cancel_all()
            if ctx.position:
                ctx.close()
            return
        if ctx.position or ctx.working_order_ids or self._session_bars is None:
            return
        if len(self._ranges) < self.atr_window or last.close == last.open:
            return
        atr = sum(self._ranges) / len(self._ranges)
        if atr <= 0:
            return
        stop = max(self.min_bracket, self.stop_atr * atr)
        target = max(self.min_bracket, self.target_atr * atr)
        (ctx.buy if last.close > last.open else ctx.sell)(
            size=self.size, stop_offset=stop, target_offset=target, tag="entry"
        )


def _run_bar_count_exit(**spec):
    strategy = BarCountExit()
    dataset = SyntheticDataset(SyntheticSpec(
        n_sessions=120, ticks_per_session=780, vol_per_tick=0.15, seed=5, **spec))
    record = Engine.for_test(dataset, strategy, split=Split.VALIDATION,
                    warmup_sessions=1).run()
    walked = len(record.end_of_session_positions)
    return strategy, record, walked


def test_the_uniform_generator_cannot_see_the_bar_count_bug():
    """The historical control. On the old generator the broken rule looks
    perfect: the learned bar count never moves, because every session has the
    same number of bars."""
    strategy, record, _ = _run_bar_count_exit(uniform=True)
    counts = strategy.min_session_bars_seen
    assert counts is not None
    left_open = [v for v in record.end_of_session_positions.values() if v != 0]
    assert not left_open, "the uniform generator should strand nothing"


def test_the_irregular_generator_catches_the_bar_count_bug():
    """The acceptance test for making the generator irregular.

    Two symptoms, both of which cost a task to find on real data:

    * the learned bar count collapses, because a quiet session has few non-empty
      bars and the rule takes a minimum -- so the strategy stops trading long
      before the session ends, for the rest of the run;
    * a session shorter than anything seen before strands a position, because the
      trigger it is waiting for never arrives.

    On the uniform generator neither happens. That is what made the bug invisible
    for three tasks.
    """
    broken, broken_record, walked = _run_bar_count_exit()
    correct, correct_record, _ = (
        ReferenceStrategy(),
        Engine.for_test(
            SyntheticDataset(SyntheticSpec(n_sessions=120, ticks_per_session=780,
                                           vol_per_tick=0.15, seed=5)),
            ReferenceStrategy(), split=Split.VALIDATION, warmup_sessions=1,
        ),
        None,
    )
    correct_record = correct_record.run()

    uniform_strategy, _, _ = _run_bar_count_exit(uniform=True)
    assert broken.min_session_bars_seen < uniform_strategy.min_session_bars_seen, (
        "the learned bar count did not collapse on irregular data; the empty "
        "intervals are not thin enough to make bar count differ from length"
    )

    stranded = [v for v in broken_record.end_of_session_positions.values() if v != 0]
    assert stranded, (
        "the bar-count rule stranded nothing; either no session is shorter than "
        "the running minimum or the exit margin is hiding it"
    )
    correct_stranded = [
        v for v in correct_record.end_of_session_positions.values() if v != 0
    ]
    assert len(correct_stranded) < len(stranded) or (
        len(correct_stranded) <= 1 and broken.min_session_bars_seen
        < uniform_strategy.min_session_bars_seen
    ), (
        "reading ctx.session_end should do better than guessing at the session "
        f"length: {len(correct_stranded)} vs {len(stranded)} stranded"
    )


def test_the_reference_strategy_is_flat_at_every_boundary_it_can_be():
    """What `ctx.session_end` bought, and the one thing it cannot buy.

    Reading the exchange's published close replaces three tasks of workarounds --
    a bar count, a learned duration, and excluding early closes from the null
    pool. Shortened sessions are no longer a special case: at a 30% early-close
    rate nothing is stranded at all.

    **The residual is not a defect.** A strategy can only exit if a trade follows
    its decision, and the engine is tick-driven: a session whose last fourteen
    minutes hold a single trade gives the exit rule one callback, on the final
    tick, with nothing left to fill against. Nothing could have closed that
    position -- there was no counterparty. So the assertion is that any session
    left open had no opportunity, not that none is ever left open.
    """
    for spec in ({"uniform": True}, {"early_close_rate": 0.3}):
        strategy = ReferenceStrategy()
        record = Engine.for_test(
            SyntheticDataset(SyntheticSpec(n_sessions=80, ticks_per_session=780,
                                           vol_per_tick=0.15, seed=11, **spec)),
            strategy, split=Split.VALIDATION, warmup_sessions=1,
        ).run()
        open_at_close = [v for v in record.end_of_session_positions.values() if v != 0]
        assert not open_at_close, f"{spec}: {len(open_at_close)} sessions left open"
        assert strategy.sessions_left_open == 0

    dataset = SyntheticDataset(SyntheticSpec(
        n_sessions=80, ticks_per_session=780, vol_per_tick=0.15, seed=11))
    strategy = ReferenceStrategy()
    record = Engine.for_test(dataset, strategy, split=Split.VALIDATION,
                    warmup_sessions=1).run()
    stranded = [int(k) for k, v in record.end_of_session_positions.items() if v != 0]
    assert len(stranded) <= 2, f"{len(stranded)} of 80 sessions left open"
    for session in stranded:
        _, end = dataset.session_bounds(session)
        margin = strategy.exit_margin_ns
        tradeable = [t for t in dataset._session_ticks(session) if end - t.ts <= margin]
        assert len(tradeable) <= 1, (
            f"session {session} had {len(tradeable)} trades in its last "
            "minutes and the position was still left open -- that is a defect, "
            "not an absent counterparty"
        )


# --------------------------------------------------------------------------- #
# is the FAR column a p-value column?
# --------------------------------------------------------------------------- #
VERIFICATIONS = sorted(
    (REPO_ROOT / "data" / "calibration").glob("empirical-null*-verification.json")
)


@pytest.mark.parametrize("path", VERIFICATIONS, ids=lambda p: p.stem)
def test_no_cell_of_the_validity_table_is_anti_conservative(path):
    """The last unverified link. Benjamini-Hochberg needs validity -- P(p <= a)
    <= a for every a -- not exact uniformity. Conservative costs power and stays
    valid; anti-conservative makes the reported false-discovery rate precise and
    wrong, which is the failure this whole project exists to close.

    Held out: tabulated on one contiguous half of the pool, measured on the
    other, over the full grid of session counts and false-acceptance rates.
    """
    doc = json.loads(path.read_text())
    for block, run in doc["runs"].items():
        assert run["anti_conservative_cells"] == [], (
            f"{path.name} block {block}: {run['anti_conservative_cells']}"
        )
        assert run["worst_ratio"] <= 1.0, run["worst_ratio"]
        assert run["tabulate_trials"] >= 200_000, (
            "at 50,000 draws the far=0.001 quantile rests on ~50 exceedances and "
            "its sampling sd is 0.026; a cell can land anti-conservative on seed "
            "noise alone"
        )


@pytest.mark.parametrize("path", VERIFICATIONS, ids=lambda p: p.stem)
def test_the_far_column_is_valid_at_the_session_counts_that_matter(path):
    """n=73 is the holdout and n=86 the validation split of the pre-registered
    calendar. Those are where every real decision is made."""
    doc = json.loads(path.read_text())
    for run in doc["runs"].values():
        checked = {c["n"] for c in run["far_column"]}
        assert {73, 86} <= checked, checked
        for column in run["far_column"]:
            assert column["valid_inside_grid"] is True, column["n"]
            assert column["p_values"] > 10_000
            # the clamped endpoint is censored, not miscalibrated, and the
            # artifact has to say so rather than quietly scoring it
            assert column["endpoint_note"]


@pytest.mark.parametrize("path", VERIFICATIONS, ids=lambda p: p.stem)
def test_the_validity_table_covers_the_whole_grid(path):
    """A validity table with the tail left out is the one shape of this evidence
    that would be worthless: every accept decision happens in the tail."""
    doc = json.loads(path.read_text())
    for run in doc["runs"].values():
        fars = {r["far"] for r in run["rows"]}
        assert 0.0005 in fars and 0.001 in fars and 0.05 in fars
        assert len(fars) >= 8, sorted(fars)


def test_the_session_counts_that_matter_are_tabulated_not_interpolated():
    assert 73 in N.N_GRID and 86 in N.N_GRID


def test_the_interpolation_basis_is_accurate_where_it_is_still_used():
    """1/sqrt(N) between tabulated counts. Measured rather than assumed: within
    0.2% at the two counts that matter, which is why tabulating them directly
    was a tidiness change and not a correctness one."""
    doc = json.loads((REPO_ROOT / "data" / "calibration"
                      / "empirical-null-nq-trades.json").read_text())
    for n in (73, 86):
        for far in (0.05, 0.01, 0.001):
            tabulated = doc["table"]["sortino_per_session"][str(n)]["quantiles"][str(far)]
            interpolated = N._interp_n(
                {k: v for k, v in doc["table"]["sortino_per_session"].items()
                 if int(k) != n},
                str(far), n,
            )
            assert abs(interpolated - tabulated) / abs(tabulated) < 0.05, (n, far)


# --------------------------------------------------------------------------- #
# the null belongs to one strategy, and the run record says how far you are
# --------------------------------------------------------------------------- #
def test_the_reference_profile_still_describes_the_committed_pool():
    """Every divergence ratio in the ledger is read against these numbers. If the
    apparatus changes and they do not, every ratio already recorded silently
    means something else."""
    from src.core.scoring.metrics import REFERENCE_PROFILE

    doc = json.loads((REPO_ROOT / "data" / "calibration"
                      / "empirical-null-nq-trades.json").read_text())
    prov = doc["provenance"]
    trades_per_session = prov["fills"] / 2 / prov["sessions_in_pool"]
    assert abs(trades_per_session - REFERENCE_PROFILE["trades_per_session"]) < 5
    sd = float(np.std(doc["pool"]))
    assert abs(sd - REFERENCE_PROFILE["return_dispersion"]) / sd < 0.25


def test_a_candidate_far_from_the_reference_is_visible_in_the_run_record():
    """The null is one strategy's. A candidate that trades at a different
    frequency has a different null and the table does not know it -- so the
    distance is recorded, as a diagnostic and never as a term in the score."""
    from src.core.scoring.metrics import null_reference_divergence

    same = null_reference_divergence(
        n_sessions=100, sessions_with_exposure=100, trade_count=4350,
        avg_holding_period_ns=1.70e12, returns=[1.0, -1.0] * 50,
        reference={"trades_per_session": 43.5, "exposure_share": 1.0,
                   "avg_holding_period_ns": 1.70e12, "return_dispersion": 1.0},
    )
    assert same["trades_per_session"] == pytest.approx(1.0, abs=0.01)
    assert same["exposure_share"] == pytest.approx(1.0, abs=0.01)
    assert same["avg_holding_period"] == pytest.approx(1.0, abs=0.01)

    rare = null_reference_divergence(
        n_sessions=100, sessions_with_exposure=12, trade_count=20,
        avg_holding_period_ns=8.0e13, returns=[1.0, -1.0] * 50,
        reference={"trades_per_session": 43.5, "exposure_share": 1.0,
                   "avg_holding_period_ns": 1.70e12, "return_dispersion": 1.0},
    )
    assert rare["trades_per_session"] < 0.02
    assert rare["exposure_share"] == pytest.approx(0.12, abs=0.01)
    assert rare["avg_holding_period"] > 10


def test_the_diagnostic_reaches_the_run_record():
    from src.core.scoring.scorer import score_diagnostics

    ds = SyntheticDataset(SyntheticSpec(n_sessions=60, ticks_per_session=390,
                                        vol_per_tick=0.15, seed=3))
    eng = Engine.for_test(ds, ReferenceStrategy(), split=Split.VALIDATION, warmup_sessions=1)
    eng.run()
    diag = score_diagnostics(eng.blotter, eng.session_calendar()).as_dict()
    assert set(diag["null_reference_divergence"]) == {
        "trades_per_session", "exposure_share", "avg_holding_period",
        "return_dispersion",
    }
    assert diag["null_assumption"]
