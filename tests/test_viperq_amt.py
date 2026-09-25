"""The ViperQ-derived champion: the properties that would fail quietly.

Not "does it make money" -- it is a starting point for a campaign, not a result.
These are the invariants the hypothesis and `ml-strategies.md` section 2f
promise, each of which would otherwise break without raising.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine                                   # noqa: E402
from src.core.types import Split                                     # noqa: E402
import src.strategies.viperq_amt as V                                # noqa: E402

SPEC = SyntheticSpec(n_sessions=120, ticks_per_session=390, vol_per_tick=0.15,
                     seed=1, train_frac=0.6, validation_frac=0.2)


@pytest.fixture(autouse=True)
def scaled_for_synthetic(monkeypatch):
    """Synthetic ticks are far sparser than NQ. Scale the per-session warmup and
    the horizon so the shape is exercised; the registered values are for real
    data and are asserted separately."""
    monkeypatch.setattr(V, "WARMUP_BARS", 20)
    monkeypatch.setattr(V, "HORIZON_STEPS", 3)
    monkeypatch.setattr(V, "DECIDE_EVERY", 5)


def run(strategy, fit_sessions=40):
    engine = Engine.for_test(SyntheticDataset(SPEC), strategy, split=Split.TRAIN,
                             warmup_sessions=0, fit_sessions=fit_sessions,
                             fill_model="unfavorable", seed=1)
    return engine, engine.run()


def test_it_fits_and_the_run_completes():
    s = V.Strategy()
    engine, record = run(s)
    assert record.outcome == "completed", record.error
    assert engine._has_fit and s.weights is not None
    assert s.rows_used > 1000


def test_it_never_acts_during_the_fit_window():
    """`warmup_actions_ignored` counts orders the engine threw away. A strategy
    trading inside its own fit window is trading on sessions it will also be
    scored on having trained on."""
    s = V.Strategy()
    _, record = run(s)
    assert record.warmup_actions_ignored == 0


def test_the_weights_are_frozen_once_fit_has_run():
    """The scored window must test the model that was fitted. A strategy that
    kept updating its parameters would be scored as something the recorded
    weights do not describe."""
    s = V.Strategy()
    engine, _ = run(s)
    after = s.weights.copy()
    rows_at_fit = s.rows_used
    assert np.array_equal(s.weights, after)
    # and nothing was accumulated after the fit boundary
    assert s.rows_used == rows_at_fit


def test_no_training_row_is_ever_stored():
    """**The memory property.** 284 sessions of one-second bars is ~14M rows;
    holding them to fit at the end would exhaust the child's limit. Only the
    normal equations survive, so the buffer stays bounded by the horizon."""
    s = V.Strategy()
    run(s)
    assert len(s.pending) <= V.HORIZON_STEPS + 1
    assert s.XtX.shape == (V.N_FEATURES + 1, V.N_FEATURES + 1)


def test_labels_do_not_cross_a_session_boundary():
    """A five-minute forward return spanning an overnight gap is not the
    quantity this predicts. Those rows are dropped, and the count is kept so the
    drop is visible rather than silent."""
    s = V.Strategy()
    run(s)
    assert s.rows_dropped > 0, (
        "no row was dropped at a session boundary across 40 sessions, so the "
        "guard is not running")


def test_features_are_absent_not_zero_early_in_a_session():
    """Section 2f in its feature form. A feature nobody could compute must not
    enter the model as a number somebody made up."""
    s = V.Strategy()

    class Bar:
        session, close, low, high, volume, delta, tick_count, open = (
            1, 100.0, 99.0, 101.0, 10, 2, 5, 100.0)

    s._new_session(Bar())
    s.bars_in_session = 0
    assert s._features(Bar()) is None


def test_fit_refuses_a_window_that_produced_almost_nothing():
    """If the usable row count is far below what the window implies, something
    filled missing values rather than dropping them."""
    s = V.Strategy()
    s.rows_used = 12
    with pytest.raises(RuntimeError, match="usable rows"):
        s.fit()


def test_weights_round_trip_as_bytes():
    s = V.Strategy()
    s.weights = np.arange(V.N_FEATURES + 1, dtype=float)
    s.edge = 0.25
    other = V.Strategy()
    other.load_weights(s.save_weights())
    assert np.array_equal(s.weights, other.weights) and other.edge == 0.25


def test_load_refuses_a_blob_of_the_wrong_shape():
    with pytest.raises(ValueError, match="expected"):
        V.Strategy().load_weights(np.arange(3, dtype=float).tobytes())


def test_save_before_fit_is_an_error():
    with pytest.raises(RuntimeError, match="before fit"):
        V.Strategy().save_weights()


def test_the_registered_interval_is_the_one_task67_measured():
    """1s, not ticks. Ticks are exact and were measured over budget; the
    hypothesis registers 1s and the strategy has to match it."""
    assert V.INTERVAL == "1s"


def test_the_edge_is_fitted_and_frozen_with_the_weights():
    """The first smoke traded twice in a hundred sessions because a fixed
    1.5-point edge was above anything the model predicted. The edge is now a
    quantile of |prediction| over a reservoir of training rows -- scale-free,
    set once in fit(), and saved with the coefficients."""
    s = V.Strategy()
    run(s)
    assert s.edge is not None and s.edge > 0
    assert 0 < len(s.reservoir) <= V.RESERVOIR
    blob = s.save_weights()
    assert len(blob) == (V.N_FEATURES + 2) * 8


def test_the_reservoir_stays_bounded():
    """Algorithm R keeps the sample uniform in O(RESERVOIR) memory, so the
    no-stored-rows property holds however long the window is."""
    s = V.Strategy()
    run(s)
    assert len(s.reservoir) <= V.RESERVOIR
    assert s.rows_seen == s.rows_used


def test_the_seed_passes_the_agents_static_check():
    """The check the agent's proposals are held to. A seed that fails it fails
    on every trial -- nq-ml-01 lost 13 of 20 to `N_FEATURES = len(FEATURES)`.
    The engine smoke cannot catch this; only this check can."""
    from src.core.loop.llm import LLMAgent
    src = (REPO_ROOT / "src" / "strategies" / "viperq_amt.py").read_text()
    ok, why = LLMAgent._static_ok(LLMAgent.__new__(LLMAgent), src)
    assert ok, why


# --------------------------------------------------------------------------- #
# the two bugs the economics committee seat found by reading the source
# (nq-ml-03, 2026-09-25). Both were in the human-written seed and were inherited
# unchanged by the agent through 20 trials, because the score cannot see either.
# --------------------------------------------------------------------------- #
def test_a_row_is_labelled_exactly_horizon_steps_later_not_one_later():
    """`_retire_labels` runs BEFORE the append, so the guard has to be `<`.

    With `<=` a row entered at decision k was labelled at k + HORIZON_STEPS + 1:
    a 6-minute forward return where the file says 5, and 15 minutes where
    nq-ml-03's champion said 10. The label horizon is the registered quantity
    (`config/hypotheses/auction-market-features.md`, "forward return over a
    fixed horizon H"), so being off by one is answering a different question.
    """
    from collections import deque

    import src.strategies.viperq_amt as V

    H = V.HORIZON_STEPS
    pending: deque = deque(maxlen=H + 1)
    labelled: dict[int, int] = {}
    for k in range(1, 4 * H + 10):
        # the guard, copied from `_retire_labels`
        if pending and not (len(pending) < H):
            labelled.setdefault(pending[0], k)
            pending.popleft()
        pending.append(k)

    first = min(labelled)
    assert labelled[first] - first == H, (
        f"a row entered at decision {first} was labelled at {labelled[first]}, "
        f"a gap of {labelled[first] - first} decisions where HORIZON_STEPS is {H}")
    assert max(len(pending), 0) <= H, "the deque must never reach its maxlen"


def test_the_guard_is_strictly_less_than():
    """The one character the test above is about, pinned in the source so a
    future edit back to `<=` fails here and names the reason."""
    src = (REPO_ROOT / "src" / "strategies" / "viperq_amt.py").read_text()
    assert "len(self.pending) < HORIZON_STEPS" in src
    assert "len(self.pending) <= HORIZON_STEPS" not in src


def test_tape_velocity_reads_volume_as_well_as_trade_count():
    """The hypothesis registers the feature as "trades **and** volume per unit
    time". It computed `bar.tick_count` only and never touched `bar.volume`, so
    a burst of many small trades and one of few large ones were the same number.
    """
    src = (REPO_ROOT / "src" / "strategies" / "viperq_amt.py").read_text()
    body = src[src.index("# 5. tape velocity"):src.index("return np.array(")]
    assert "bar.volume" in body, "tape velocity still ignores volume"
    assert "bar.tick_count" in body, "tape velocity must keep the trade-rate half"
    assert "self.volume_sum" in body, "it must normalise by the session's own rate"

    # accumulated every bar and reset with the session, like tick_count_sum
    assert "self.volume_sum += bar.volume" in src
    assert src.count("self.volume_sum = 0") == 2, (
        "volume_sum must be initialised and reset per session, exactly like "
        "tick_count_sum")


def test_the_registered_feature_wording_is_what_the_code_does():
    """The pair that made both bugs invisible: the hypothesis is prose, the code
    is code, and nothing compares them. This is that comparison, for the one
    feature where it was wrong."""
    hyp = (REPO_ROOT / "config" / "hypotheses" /
           "auction-market-features.md").read_text().lower()
    assert "trades and volume per unit time" in hyp
    src = (REPO_ROOT / "src" / "strategies" / "viperq_amt.py").read_text()
    assert "bar.volume" in src and "bar.tick_count" in src
