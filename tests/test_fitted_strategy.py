"""Fitted strategies: the fit window, and the refusal that protects validation.

Design: `docs/design/ml-strategies.md`. The valuable assertions are that `fit()` is
called exactly once at the edge of the warmup window, that it refuses afterwards,
and that the fit window is absent from the scoring calendar -- which is what stops
Amendment 3's idle charge billing every session the strategy was required not to
trade in.
"""

from __future__ import annotations

import pytest

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec
from src.core.engine import Engine
from src.core.engine.engine import validate_fitted_strategy
from src.core.types import Split


def ds(n_sessions=40):
    return SyntheticDataset(SyntheticSpec(
        n_sessions=n_sessions, ticks_per_session=120, vol_per_tick=0.15,
        seed=4_242, signal_strength=0.0))


class Fitted:
    """Accumulates during warmup, fits at the edge, then trades on the fitted mean."""

    def __init__(self):
        self.seen: list[float] = []
        self.fits = 0
        self.mean: float | None = None
        self.sessions_seen_at_fit: int | None = None
        self.traded_sessions: set[int] = set()

    def setup(self, ctx): ctx.subscribe("5m")

    def on_bars(self, ctx):
        h = ctx.history("5m")
        if not h:
            return
        self.seen.append(h[-1].close)
        if self.mean is None:          # still accumulating
            return
        if ctx.position or ctx.working_order_ids:
            return
        self.traded_sessions.add(h[-1].session)
        place = ctx.buy if h[-1].close > self.mean else ctx.sell
        place(size=1, stop_offset=0.75, target_offset=0.75, tag="f")

    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass

    def fit(self):
        self.fits += 1
        self.sessions_seen_at_fit = len(self.seen)
        self.mean = sum(self.seen) / len(self.seen)

    def save_weights(self): return str(self.mean).encode()
    def load_weights(self, blob): self.mean = float(blob.decode())


def _run(strategy, fit_sessions, n_sessions=40):
    eng = Engine.for_test(ds(n_sessions), strategy, split=Split.TRAIN,
                          warmup_sessions=0, fit_sessions=fit_sessions,
                          fill_model="unfavorable", seed=1)
    eng.run()
    return eng


# --------------------------------------------------------------------------- #
# the fit window
# --------------------------------------------------------------------------- #

def test_fit_is_called_exactly_once_at_the_edge_of_warmup():
    s = Fitted()
    eng = _run(s, fit_sessions=10)
    assert s.fits == 1, "fit() must be called once per run, at the boundary"
    assert s.mean is not None
    assert s.sessions_seen_at_fit > 0, (
        "fit() saw no accumulated data -- it was called before the warmup window "
        "was walked, not at its edge")


def test_the_strategy_cannot_trade_before_it_has_fitted():
    """Warmup already ignores actions; this asserts the fitted path inherits it,
    so a model cannot trade on an unfitted mean."""
    s = Fitted()
    eng = _run(s, fit_sessions=10)
    scored = set(eng.scored_sessions())
    assert s.traded_sessions, "the fixture should trade after fitting"
    assert s.traded_sessions <= scored, (
        f"traded in {sorted(s.traded_sessions - scored)}, which are warmup sessions")


def test_the_fit_window_is_absent_from_the_scoring_calendar():
    """The consequence that matters for Amendment 3. A fitted strategy is required
    not to trade during the fit window; if those sessions were in the calendar the
    idle charge would bill every one of them and the score would be an artefact of
    the fit length."""
    s = Fitted()
    eng = _run(s, fit_sessions=10, n_sessions=40)
    cal = eng.session_calendar()
    assert len(cal) == len(eng.scored_sessions())
    assert len(cal) < len(eng._split_sessions()), "the fit window is still being scored"
    assert set(eng._fit_set()).isdisjoint(eng.scored_sessions())
    from src.core.scoring.metrics import idle_charges
    charged = idle_charges(eng.reconstructed(), cal)
    assert len(charged) == len(cal), (
        "the idle charge is being computed over more sessions than are scored")


def test_a_second_fit_is_refused():
    """The refusal protects the validation split: --stage validate runs the winner's
    SOURCE, and a fit() that fired there would retrain on validation."""
    s = Fitted()
    eng = _run(s, fit_sessions=10)
    with pytest.raises(RuntimeError, match="already been called"):
        eng._fit_now()


def test_a_fitted_strategy_with_no_warmup_is_refused_at_wiring_time():
    """Not at trial 40. With no fit window it would fit on nothing and trade on
    everything, which is a silently meaningless run.

    The window is a prefix of the SPLIT, not the warmup window: warmup lives
    before the split and TRAIN -- where campaigns run -- has none at all."""
    with pytest.raises(TypeError, match="neither fit nor load"):
        Engine.for_test(ds(), Fitted(), split=Split.TRAIN, fit_sessions=0,
                        fill_model="unfavorable", seed=1)


def test_a_plain_strategy_may_not_be_given_a_fit_window():
    class Plain:
        def setup(self, ctx): ctx.subscribe("5m")
        def on_bars(self, ctx): pass
        def on_fill(self, ctx): pass
        def on_lifecycle(self, ctx): pass
    with pytest.raises(TypeError, match="not a fitted strategy"):
        Engine.for_test(ds(), Plain(), split=Split.TRAIN, fit_sessions=10,
                        fill_model="unfavorable", seed=1)


def test_a_plain_strategy_is_untouched_by_any_of_this():
    """The fitted path must be invisible to every strategy that is not fitted."""
    class Plain:
        def setup(self, ctx): ctx.subscribe("5m")
        def on_bars(self, ctx): pass
        def on_fill(self, ctx): pass
        def on_lifecycle(self, ctx): pass
    eng = _run(Plain(), fit_sessions=0)
    assert eng._needs_fit is False and eng._has_fit is False


# --------------------------------------------------------------------------- #
# the contract
# --------------------------------------------------------------------------- #

def test_the_fitted_contract_is_checked_at_wiring_time():
    class Missing(Fitted):
        load_weights = None
    with pytest.raises(TypeError, match="load_weights"):
        validate_fitted_strategy(Missing())

    class WrongArity(Fitted):
        def fit(self, ctx): pass
    with pytest.raises(TypeError, match=r"fit\(ctx\) does not take exactly 0"):
        validate_fitted_strategy(WrongArity())


def test_weights_round_trip(tmp_path):
    s = Fitted()
    _run(s, fit_sessions=10)
    blob = s.save_weights()
    other = Fitted()
    other.load_weights(blob)
    assert other.mean == s.mean


# --------------------------------------------------------------------------- #
# weights as an artifact
# --------------------------------------------------------------------------- #

def test_weights_are_recorded_by_digest_and_not_globbed(tmp_path):
    """They are an OUTPUT, so they follow `fill_log_digest`, not the prompt set.

    ARTIFACT_GLOBS is compared at load and again after the run. Weights are written
    during the run, at the fit boundary, so globbing them would make every fitted
    trial invalidate itself -- which is exactly what a stray file in
    config/hypotheses/ did to nq-orderflow-02.
    """
    from src.core.scoring.integrity import ARTIFACT_GLOBS
    assert not any("weights" in g for g in ARTIFACT_GLOBS), (
        "weights are in ARTIFACT_GLOBS; every fitted trial will now invalidate "
        "itself the moment fit() writes them")


def test_store_weights_mirrors_store_source(tmp_path):
    from src.core.runlog import store_weights

    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("")
    s = Fitted()
    _run(s, fit_sessions=10)
    rel = store_weights(ledger, "abc123", s.save_weights())
    assert rel, "store_weights returned nothing for a fitted strategy"
    out = tmp_path / "weights" / "abc123.weights"
    assert out.exists() and out.read_text() == str(s.mean)


def test_store_weights_is_silent_when_there_is_nothing_to_store(tmp_path):
    """Returns "" rather than raising, so every run path can call it unguarded.

    A plain strategy has no weights to hand over, so the caller passes none --
    `attach_weights` reaches this only for a run that actually fitted."""
    from src.core.runlog import store_weights

    ledger = tmp_path / "ledger.jsonl"; ledger.write_text("")
    assert store_weights(ledger, "abc123", b"") == ""
    assert store_weights(ledger, "", b"some weights") == ""


def test_weights_are_not_written_when_the_run_never_fitted(tmp_path):
    """A crash before the fit boundary must leave no file. An empty weights file
    is worse than none: a later load would succeed and give an untrained model."""
    from src.core.runlog import store_weights

    ledger = tmp_path / "ledger.jsonl"; ledger.write_text("")

    # An empty blob is recorded as absent rather than as an empty file a later
    # run would happily load as a trained model.
    assert store_weights(ledger, "abc123", b"") == ""
    assert not (tmp_path / "weights" / "abc123.weights").exists()


# --------------------------------------------------------------------------- #
# the whole chain, with the reference strategy
# --------------------------------------------------------------------------- #

def test_the_fitted_reference_runs_the_whole_chain():
    """Fit window walked, fit() once at its edge, scored window disjoint from it,
    a real score, and weights that survive a round trip.

    Uses `FittedReference` rather than the fixture above because it is the thing a
    real fitted strategy will look like -- it accumulates a feature through
    `on_bars`, fits a statistic of the fit window, and trades it afterwards.
    """
    from src.core.scoring.scorer import score
    from src.strategies.fitted_reference import FittedReference

    s = FittedReference()
    eng = Engine.for_test(ds(200), s, split=Split.TRAIN, warmup_sessions=0,
                          fit_sessions=60, fill_model="unfavorable", seed=1)
    eng.run()

    assert eng._has_fit and s.threshold is not None
    assert len(eng._fit_set()) == 60
    assert set(eng._fit_set()).isdisjoint(eng.scored_sessions())
    cal = eng.session_calendar()
    assert len(cal) == len(eng._split_sessions()) - 60

    out = score(eng.reconstructed(), period=cal)
    assert out.trade_count > 0, "the reference should trade after fitting"
    assert not out.rejected


def test_the_reference_refuses_to_fit_on_nothing():
    """If the engine ever called fit() before walking the window, this is the
    error a reader should get -- not a threshold quietly fitted to an empty list."""
    from src.strategies.fitted_reference import FittedReference

    with pytest.raises(RuntimeError, match="nothing accumulated"):
        FittedReference().fit()


def test_the_reference_will_not_trade_before_it_is_fitted():
    """`threshold is None` is checked rather than assumed. A strategy that traded
    on an unfitted parameter would silently be a different strategy."""
    from src.strategies.fitted_reference import FittedReference

    s = FittedReference()
    assert s.threshold is None
    calls = []

    class Ctx:
        position = 0
        working_order_ids = ()
        def history(self, _):
            class B: delta = 10_000; close = 1.0
            return [B()]
        def buy(self, **kw): calls.append("buy")
        def sell(self, **kw): calls.append("sell")

    s.on_bars(Ctx())
    assert calls == [], "traded before fit() was called"
    assert s._seen, "should still have accumulated the bar"


# --------------------------------------------------------------------------- #
# loading weights instead of fitting -- the protection section 2a exists for
# --------------------------------------------------------------------------- #

def test_a_run_given_weights_loads_them_and_never_fits(tmp_path):
    """This is the whole point. `--stage validate` runs the winner's SOURCE on the
    validation split; a fit() firing there would train on the split it is about to
    be scored against."""
    from src.strategies.fitted_reference import FittedReference

    trained = FittedReference()
    Engine.for_test(ds(200), trained, split=Split.TRAIN, fit_sessions=60,
                    fill_model="unfavorable", seed=1).run()
    w = tmp_path / "champion.weights"
    w.write_bytes(trained.save_weights())

    loaded = FittedReference()
    eng = Engine.for_test(ds(200), loaded, split=Split.VALIDATION,
                          weights_path=w, fill_model="unfavorable", seed=1)
    assert loaded.threshold == trained.threshold, "weights were not loaded"
    assert eng._has_fit and eng._loaded_weights
    assert eng.fit_sessions == 0
    eng.run()
    # It ran a whole split and never fitted: the threshold is still the trained one.
    assert loaded.threshold == trained.threshold, "the run refitted on this split"
    assert not eng._fit_set(), "a loading run must not carve a fit window"


def test_a_missing_weights_file_is_an_error_not_a_refit(tmp_path):
    from src.strategies.fitted_reference import FittedReference

    with pytest.raises(FileNotFoundError, match="not a licence to refit"):
        Engine.for_test(ds(), FittedReference(), split=Split.VALIDATION,
                        weights_path=tmp_path / "absent.weights",
                        fill_model="unfavorable", seed=1)


def test_fitting_and_loading_in_the_same_run_is_refused(tmp_path):
    """Doing both would discard the loaded weights and retrain on this split."""
    from src.strategies.fitted_reference import FittedReference

    trained = FittedReference()
    Engine.for_test(ds(200), trained, split=Split.TRAIN, fit_sessions=60,
                    fill_model="unfavorable", seed=1).run()
    w = tmp_path / "w.weights"; w.write_bytes(trained.save_weights())
    with pytest.raises(TypeError, match="either fits or loads"):
        Engine.for_test(ds(200), FittedReference(), split=Split.TRAIN,
                        fit_sessions=60, weights_path=w,
                        fill_model="unfavorable", seed=1)
