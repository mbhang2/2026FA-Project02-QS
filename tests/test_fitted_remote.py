"""Fitted strategies across the process boundary.

`tests/test_fitted_strategy.py` proves the fit window, the single call and the
refusal to refit -- all of it **in process**, through `Engine.for_test`. That is
not how a scored run works: `run.py` hands the engine a proxy and the strategy
lives in a child process, and until this module existed the fitted path simply
did not engage there. See `docs/design/ml-strategies.md` section 11.

The property every test here circles is the same one: **an ordinary strategy must
be unable to tell that any of this was added.**
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.engine.engine import FIT_ARITY, Engine, Split  # noqa: E402
from src.core.engine.remote import (  # noqa: E402
    HarnessFailed, RemoteFittedStrategy, RemoteStrategy,
)
from src.core.engine.remote import child as childmod  # noqa: E402
from tests.test_remote import ds  # noqa: E402

FITTED = '''
class Strategy:
    def __init__(self):
        self.seen = []
        self.threshold = None
    def setup(self, ctx): ctx.subscribe('5m')
    def on_bars(self, ctx):
        h = ctx.history('5m')
        if h:
            self.seen.append(abs(h[-1].delta))
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass
    def fit(self):
        self.threshold = float(sorted(self.seen)[int(0.8 * len(self.seen))]) \\
            if self.seen else 0.0
    def save_weights(self): return repr(self.threshold).encode()
    def load_weights(self, blob): self.threshold = float(blob.decode())
'''

PLAIN = '''
class Strategy:
    def setup(self, ctx): ctx.subscribe('5m')
    def on_bars(self, ctx): pass
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass
'''


def _write(tmp_path, body, name="strat.py"):
    path = tmp_path / name
    path.write_text(body)
    return path


# --------------------------------------------------------------------------- #
# 1. the gap section 11 found
# --------------------------------------------------------------------------- #
def test_a_fitted_strategy_now_fits_through_the_child(tmp_path):
    """The whole point. Before this, `run.py` raised at Engine construction:
    the proxy had no `fit`, so `hasattr(strategy, "fit")` was False and a profile
    with `fit_sessions` set died with "RemoteStrategy is not a fitted strategy"."""
    path = _write(tmp_path, FITTED)
    with RemoteFittedStrategy(path, "Strategy", sandbox=False) as remote:
        engine = Engine.for_test(ds(n_sessions=60, ticks_per_session=120),
                                 remote, split=Split.VALIDATION,
                                 warmup_sessions=1, fit_sessions=5, seed=1)
        engine.run()
        assert engine._has_fit, "fit() never reached the child"


def test_the_fit_window_is_still_absent_from_the_scored_calendar(tmp_path):
    """The in-process guarantee has to survive the crossing: sessions the model
    trained on cannot also be sessions it is graded on."""
    path = _write(tmp_path, FITTED)
    with RemoteFittedStrategy(path, "Strategy", sandbox=False) as remote:
        engine = Engine.for_test(ds(n_sessions=60, ticks_per_session=120),
                                 remote, split=Split.VALIDATION,
                                 warmup_sessions=1, fit_sessions=5, seed=1)
        engine.run()
        scored = set(engine.scored_sessions())
        assert scored, "nothing was scored at all"
        assert not scored & set(engine._fit_set()), (
            "a session was both trained on and scored")


# --------------------------------------------------------------------------- #
# 2. the ordinary path must not notice
# --------------------------------------------------------------------------- #
def test_the_plain_proxy_still_advertises_no_fit():
    """The regression that would corrupt every non-fitted score. `Engine` decides
    a strategy is fitted with `all(hasattr(strategy, m) for m in FIT_ARITY)`, so
    methods added to `RemoteStrategy` itself -- rather than to the subclass --
    would carve a fit window out of every ordinary run."""
    for method in FIT_ARITY:
        assert not hasattr(RemoteStrategy, method), (
            f"RemoteStrategy grew {method}(); every ordinary strategy now looks "
            "fitted to the engine")


def test_a_plain_strategy_runs_through_the_plain_proxy_unchanged(tmp_path):
    path = _write(tmp_path, PLAIN)
    with RemoteStrategy(path, "Strategy", sandbox=False) as remote:
        engine = Engine.for_test(ds(n_sessions=40, ticks_per_session=120),
                                 remote, split=Split.VALIDATION,
                                 warmup_sessions=1, seed=1)
        engine.run()
        assert not getattr(engine, "_needs_fit", False)


# --------------------------------------------------------------------------- #
# 3. the cross-check, in both directions
# --------------------------------------------------------------------------- #
def test_a_class_with_fit_run_as_an_ordinary_strategy_is_refused(tmp_path):
    """**The silent direction, and the reason `_check_capabilities` exists.**

    A profile that omits `fit_sessions` for a class that has `fit()` would build
    the plain proxy, never engage the fitted path, never call `fit()`, and score
    a model carrying whatever its constructor left behind. Nothing would raise
    and the row would look exactly like a real result."""
    path = _write(tmp_path, FITTED)
    with pytest.raises(HarnessFailed, match="defines fit"):
        RemoteStrategy(path, "Strategy", sandbox=False).__enter__()


def test_a_class_without_fit_run_as_a_fitted_strategy_is_refused(tmp_path):
    path = _write(tmp_path, PLAIN)
    with pytest.raises(HarnessFailed, match="defines no fit"):
        RemoteFittedStrategy(path, "Strategy", sandbox=False).__enter__()


def test_the_child_reports_what_the_class_defines_not_what_was_asked(tmp_path):
    path = _write(tmp_path, FITTED)
    with RemoteFittedStrategy(path, "Strategy", sandbox=False) as remote:
        assert set(remote._capabilities) == set(FIT_ARITY)


# --------------------------------------------------------------------------- #
# 4. drift between the two spellings of the protocol
# --------------------------------------------------------------------------- #
def test_the_childs_copy_of_the_protocol_matches_the_engines():
    """`child.py` must not import the engine -- that separation is the isolation
    -- so `FIT_METHODS` is spelled there a second time. Drift would mean the child
    advertises a capability the engine does not enforce, or misses one it does."""
    assert set(childmod.FIT_METHODS) == set(FIT_ARITY), (
        f"child.FIT_METHODS {childmod.FIT_METHODS} has drifted from "
        f"engine.FIT_ARITY {sorted(FIT_ARITY)}")


def test_the_child_still_does_not_import_the_engine():
    source = (REPO_ROOT / "src" / "core" / "engine" / "remote" / "child.py").read_text()
    assert "from src.core.engine.engine" not in source
    assert "import engine" not in source


# --------------------------------------------------------------------------- #
# 5. weights across the boundary, and the controls on them
# --------------------------------------------------------------------------- #
def test_weights_round_trip_through_the_child(tmp_path):
    """Fit in one child, carry the bytes out, load them into a different child.

    This is the chain a champion takes to validation: the second process never
    fits, and it ends up with the parameters the first one learned."""
    path = _write(tmp_path, FITTED)
    with RemoteFittedStrategy(path, "Strategy", sandbox=False) as remote:
        engine = Engine.for_test(ds(n_sessions=60, ticks_per_session=120),
                                 remote, split=Split.VALIDATION,
                                 warmup_sessions=1, fit_sessions=5, seed=1)
        engine.run()
        blob = remote.save_weights()
    assert blob, "the fitted strategy handed back nothing"

    weights = tmp_path / "w.weights"
    weights.write_bytes(blob)
    with RemoteFittedStrategy(path, "Strategy", sandbox=False) as second:
        second.load_weights(weights.read_bytes())
        again = second.save_weights()
    assert again == blob, "a loaded model does not match the one that was saved"


def test_save_weights_returns_bytes_not_a_path(tmp_path):
    """The API had to change: a strategy runs where `file-write*` is denied, so
    one handed a path to write could not use it."""
    path = _write(tmp_path, FITTED)
    with RemoteFittedStrategy(path, "Strategy", sandbox=False) as remote:
        engine = Engine.for_test(ds(n_sessions=60, ticks_per_session=120),
                                 remote, split=Split.VALIDATION,
                                 warmup_sessions=1, fit_sessions=5, seed=1)
        engine.run()
        assert isinstance(remote.save_weights(), bytes)


def test_a_second_save_in_one_run_is_refused(tmp_path):
    path = _write(tmp_path, FITTED)
    with RemoteFittedStrategy(path, "Strategy", sandbox=False) as remote:
        engine = Engine.for_test(ds(n_sessions=60, ticks_per_session=120),
                                 remote, split=Split.VALIDATION,
                                 warmup_sessions=1, fit_sessions=5, seed=1)
        engine.run()
        remote.save_weights()
        with pytest.raises(HarnessFailed, match="twice in one run"):
            remote.save_weights()


def test_an_oversized_blob_is_refused_before_it_is_decoded(tmp_path):
    """The cap is checked on the encoded payload. A child claiming a gigabyte
    must not be able to make the parent allocate one."""
    path = _write(tmp_path, FITTED)
    with RemoteFittedStrategy(path, "Strategy", sandbox=False,
                              weights_cap_bytes=4) as remote:
        engine = Engine.for_test(ds(n_sessions=60, ticks_per_session=120),
                                 remote, split=Split.VALIDATION,
                                 warmup_sessions=1, fit_sessions=5, seed=1)
        engine.run()
        with pytest.raises(HarnessFailed, match="over the 4-byte cap"):
            remote.save_weights()


def test_the_parent_never_deserializes_the_weights():
    """**The control that matters most, and the one the obvious implementation
    gets wrong.** Validating weights means parsing them; parsing a pickle means
    arbitrary code execution in the parent, outside the sandbox -- a worse hole
    than the boundary closes. The parent writes bytes and hashes bytes. Only the
    child, already sandboxed, ever deserializes."""
    for rel in ("src/core/runlog.py", "run.py"):
        source = (REPO_ROOT / rel).read_text()
        for danger in ("pickle.load", "pickle.loads", "torch.load",
                       "allow_pickle=True", "joblib.load", "yaml.load("):
            assert danger not in source, (
                f"{rel} calls {danger} -- weights reaching this process are bytes "
                "chosen inside the sandbox, and parsing them here executes them "
                "outside it")


def test_the_engine_hands_load_weights_bytes_not_a_path():
    source = (REPO_ROOT / "src" / "core" / "engine" / "engine.py").read_text()
    assert "strategy.load_weights(wp.read_bytes())" in source, (
        "the engine passes something other than bytes to load_weights; the "
        "child cannot open a path")


# --------------------------------------------------------------------------- #
# 6. the champion handoff -- section 5's validation look, for a fitted winner
# --------------------------------------------------------------------------- #
def test_the_validation_profiles_differ_only_by_the_weights():
    """`nq-validation-fitted` exists because `weights_path` cannot sit on
    `nq-validation` unconditionally: the engine treats a missing weights file as
    an error, correctly, since absence is not permission to refit. But every
    other evaluation condition has to match, or the fitted champion is scored
    under different costs than the unfitted one and the two are not comparable --
    the failure Task 61 found when validation silently charged 0.01 against
    train's 0.10."""
    import yaml

    profiles = yaml.safe_load((REPO_ROOT / "config" / "run.yaml").read_text())["profiles"]
    plain, fitted = profiles["nq-validation"], profiles["nq-validation-fitted"]
    differ = {k for k in set(plain) | set(fitted) if plain.get(k) != fitted.get(k)}
    assert differ == {"weights_path"}, (
        f"the two validation profiles differ in {differ}; they may differ only "
        "in the weights")


def test_the_validate_stage_carries_weights_and_cleans_up():
    """The gap this closed. `stage_validate` swapped only `current.py`, so a
    fitted champion arrived at validation with its source and none of its learned
    parameters."""
    source = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()
    assert 'win.get("weights_source")' in source, (
        "stage_validate does not read the winner's weights; a fitted champion "
        "would be validated without the parameters it fitted")
    assert "VALIDATION_PROFILE_FITTED" in source
    assert "live_weights.unlink(missing_ok=True)" in source, (
        "the transient weights file is not removed; a later unfitted validate "
        "would find a stale champion's parameters sitting there")


def test_a_champion_that_lost_its_weights_file_is_refused():
    """Refitting on the validation split is the one thing this stage exists to
    prevent, so a recorded-but-missing weights file must stop the run rather
    than fall back to the unfitted profile."""
    source = (REPO_ROOT / "scripts" / "nq_campaign.py").read_text()
    assert "is not there. A fitted champion cannot be validated" in source


def test_loading_weights_means_the_run_cannot_also_fit(tmp_path):
    """`weights_path` and `fit_sessions` together would discard the loaded model
    and retrain on the split being scored. The engine refuses the combination;
    this is the assertion that it still does after the API change."""
    path = _write(tmp_path, FITTED)
    with RemoteFittedStrategy(path, "Strategy", sandbox=False) as remote:
        engine = Engine.for_test(ds(n_sessions=60, ticks_per_session=120),
                                 remote, split=Split.VALIDATION,
                                 warmup_sessions=1, fit_sessions=5, seed=1)
        engine.run()
        blob = remote.save_weights()
    w = tmp_path / "champion.weights"
    w.write_bytes(blob)

    with RemoteFittedStrategy(path, "Strategy", sandbox=False) as remote:
        with pytest.raises(TypeError, match="either fits or loads"):
            Engine.for_test(ds(n_sessions=60, ticks_per_session=120), remote,
                            split=Split.VALIDATION, warmup_sessions=1,
                            fit_sessions=5, weights_path=w, seed=1)

    with RemoteFittedStrategy(path, "Strategy", sandbox=False) as remote:
        engine = Engine.for_test(ds(n_sessions=60, ticks_per_session=120), remote,
                                 split=Split.VALIDATION, warmup_sessions=1,
                                 weights_path=w, seed=1)
        engine.run()
        assert engine._loaded_weights and engine._has_fit
        assert remote.save_weights() == blob, (
            "the validation run is not carrying the champion's parameters")


# --------------------------------------------------------------------------- #
# 7. the weights actually reach the disk
# --------------------------------------------------------------------------- #
def test_attach_weights_does_not_depend_on_a_digest_the_record_lacks(tmp_path):
    """**Regression: a fitted run trained, crossed the boundary, and dropped its
    weights on the floor.**

    `_row` adds `strategy_digest` when it assembles the final ledger row, which
    is after every `attach_weights` call site. Reading the digest off the record
    therefore found "", `store_weights` returned "" from its `not digest` guard,
    and the row carried `weights_source: ""`. Nothing raised. The whole suite
    passed, because no unit test builds a record through `_row` -- it took the
    first real-NQ smoke to surface it.
    """
    import run as runpy

    class Engine:
        _has_fit = True
        _fit_completed = True        # fit() returned; the real engine sets this after it
        _loaded_weights = False
        fit_sessions = 284
        weights_path = None

        class strategy:
            @staticmethod
            def save_weights():
                return b"3.5\n"

    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("")
    record = runpy.attach_weights({}, Engine(), ledger)   # no strategy_digest

    assert record["weights_source"], (
        "a fitted run recorded no weights source; the weights were computed, "
        "handed across the boundary, and silently discarded")
    written = tmp_path / "weights"
    files = list(written.glob("*.weights"))
    assert len(files) == 1 and files[0].read_bytes() == b"3.5\n"
    assert record["fit_sessions"] == 284


def test_a_fit_that_raised_leaves_its_row_an_outcome_not_a_second_exception(tmp_path):
    """**Regression, 2026-09-25: a fitted strategy that raised inside `fit()` wrote
    a ledger row with no outcome.**

    `_has_fit` is set before `fit()` is called so a re-entrant call raises. After
    a crash inside `fit()` it is therefore True, `attach_weights` asked the dead
    child for `save_weights()`, the pipe raised `HarnessFailed` from inside the
    crash handler, and the row was written from an empty record -- the Task 36
    load-failure hole, in a new place. Found by running `viperq_amt` on a
    synthetic fit window too short for it.
    """
    import run as runpy

    class Engine:
        _has_fit = True
        _fit_completed = False       # entered fit(), never returned from it
        _loaded_weights = False
        fit_sessions = 20
        weights_path = None

        class strategy:
            @staticmethod
            def save_weights():
                raise AssertionError("must not be asked: the child is gone")

    record = {"outcome": "crashed"}
    out = runpy.attach_weights(record, Engine(), tmp_path / "ledger.jsonl")
    assert out["outcome"] == "crashed"
    assert out["fit_completed"] is False and out["weights_source"] == ""

    class Exploding(Engine):
        _fit_completed = True

        class strategy:
            @staticmethod
            def save_weights():
                raise RuntimeError("the pipe failed")

    # The handler-side wrapper: whatever goes wrong persisting weights on a
    # failure path is recorded on the row, never raised past the handler.
    record = {"outcome": "crashed"}
    out = runpy._attach_weights_safely(record, Exploding(), tmp_path / "ledger.jsonl")
    assert out["outcome"] == "crashed"
    assert "the pipe failed" in out["weights_error"]
