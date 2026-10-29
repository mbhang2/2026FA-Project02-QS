"""`harness_alpha`: Nalin's vectorised AST library, used from a streaming callback.

The three things the bridge has to get right, each with a test that fails if
it stops being right:

1. evaluate on a trailing window, not the whole history (affordability);
2. one bar of lag, not two (correctness, and the subtle one);
3. a continuous weight becomes whole contracts without churning.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _bridge():
    """Load it the way the child does: by file path, under a bare name."""
    if "harness_alpha" in sys.modules:
        return sys.modules["harness_alpha"]
    spec = importlib.util.spec_from_file_location(
        "harness_alpha", REPO_ROOT / "src/core/engine/remote/alpha_env.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["harness_alpha"] = m
    spec.loader.exec_module(m)
    return m


class _Bar:
    __slots__ = ("open", "high", "low", "close", "volume")

    def __init__(self, c, v=100.0):
        self.open = self.high = self.low = self.close = float(c)
        self.volume = float(v)


ZSCORE = {"type": "operator", "name": "ts_zscore",
          "args": [{"type": "field", "name": "close"}],
          "kwargs": {"window": 20}}


# --------------------------------------------------------------- 1. window
def test_the_bridge_keeps_only_the_window_it_needs():
    """The whole reason it is affordable. Holding full history would make a
    late bar cost what bar 20,000 costs."""
    m = _bridge()
    a = m.Alpha(ZSCORE)
    a.push([_Bar(100 + i) for i in range(5_000)])
    assert len(a._rows) == a.lookback <= 32, (
        f"the bridge retained {len(a._rows)} rows; it must keep only its "
        f"lookback or the per-bar cost grows with the session")


def test_a_window_too_short_for_the_ast_is_refused_not_silently_nan():
    m = _bridge()
    with pytest.raises(ValueError, match="shorter than"):
        m.Alpha(ZSCORE, lookback=5)


def test_lookback_sums_down_a_branch_because_operators_compose():
    """A window sized to the largest single node emits NaN for the first
    thirty bars and reads as a broken signal."""
    m = _bridge()
    nest = {"type": "operator", "name": "ts_zscore", "args": [
        {"type": "operator", "name": "ts_delta", "args": [
            {"type": "operator", "name": "ts_ema",
             "args": [{"type": "field", "name": "close"}],
             "kwargs": {"span": 10}}], "kwargs": {"periods": 1}}],
        "kwargs": {"window": 20}}
    assert m.required_lookback(nest) == 31        # 20 + 1 + 10
    assert m.required_lookback(ZSCORE) == 20


def test_an_ast_naming_a_field_a_bar_lacks_fails_at_construction():
    m = _bridge()
    bad = {"type": "operator", "name": "ts_mean",
           "args": [{"type": "field", "name": "vwap"}], "kwargs": {"window": 5}}
    with pytest.raises(ValueError, match="fields a bar does not have"):
        m.Alpha(bad)


# ------------------------------------------------------------------ 2. lag
def test_the_bridge_applies_one_bar_of_lag_and_not_two():
    """**The subtle one, and the reason the bridge exists at all.**

    `compile_strategy_weights` ends in `.shift(1)`, which is correct in his
    vectorised convention and wrong here, because this engine already supplies
    that bar: `on_bars` at bar t sees bars through t and the fill lands after
    t closes. Reading his shifted series inside a callback composes the two.

    So the bridge's weight after seeing bars 0..t must equal `tanh(alpha_t)`,
    NOT `tanh(alpha_(t-1))`.
    """
    from src.primitives.parser import compile_strategy_weights, evaluate_node

    m = _bridge()
    rng = np.random.default_rng(3)
    close = 100 + np.cumsum(rng.normal(0, 1.0, 400))
    frame = pd.DataFrame({"open": close, "high": close, "low": close,
                          "close": close, "volume": np.full(400, 100.0)})

    a = m.Alpha(ZSCORE)
    a.push([_Bar(c) for c in close])
    got = a.weight()

    alpha = evaluate_node(ZSCORE, frame)
    unshifted = float(np.tanh(alpha.iloc[-1]))
    shifted = float(compile_strategy_weights(ZSCORE, frame).iloc[-1])

    assert got == pytest.approx(unshifted, abs=1e-9), (
        "the bridge is not reporting tanh(alpha) on the NEWEST bar")
    assert abs(got - shifted) > 1e-6, (
        "the bridge agrees with compile_strategy_weights, which means it "
        "picked up his shift on top of the engine's own bar -- two bars of lag")


def test_the_trailing_window_gives_the_same_weight_as_full_history():
    """Affordability must not cost accuracy: a window at least as long as the
    AST needs makes the trailing evaluation identical to the whole frame."""
    from src.primitives.parser import evaluate_node

    m = _bridge()
    rng = np.random.default_rng(5)
    close = 100 + np.cumsum(rng.normal(0, 1.0, 600))
    frame = pd.DataFrame({"open": close, "high": close, "low": close,
                          "close": close, "volume": np.full(600, 100.0)})
    a = m.Alpha(ZSCORE)
    a.push([_Bar(c) for c in close])
    full = float(np.tanh(evaluate_node(ZSCORE, frame).iloc[-1]))
    assert a.weight() == pytest.approx(full, abs=1e-9)


def test_warming_up_reports_none_rather_than_flat():
    """"I cannot say yet" and "flat is correct" are different, and a strategy
    that cannot tell them apart trades the warm-up."""
    m = _bridge()
    a = m.Alpha(ZSCORE)
    a.push([_Bar(100 + i) for i in range(5)])
    assert a.weight() is None
    assert a.target(position=0) is None
    a.push([_Bar(100 + i) for i in range(5, 40)])
    assert a.weight() is not None


# ------------------------------------------------------------ 3. contracts
def test_the_deadband_stops_a_boundary_signal_from_churning():
    """A weight hovering near a rounding boundary would flip the position
    every bar, paying the spread for a signal that never changed its mind."""
    m = _bridge()
    a = m.Alpha(ZSCORE, max_contracts=1, deadband=0.15)
    # a target 0.52 contracts away from flat rounds to 1 with no band...
    assert m.Alpha(ZSCORE, deadband=0.0)._round(0.52, 0) == 1
    # ...and stays flat inside the band
    assert a._round(0.52, 0) == 1          # 0.52 > 0.15 from 0: it moves
    assert a._round(0.10, 0) == 0          # 0.10 <= 0.15 from 0: it does not
    assert a._round(0.92, 1) == 1          # already long 1, 0.08 away: holds


def test_max_contracts_scales_the_target_and_is_refused_below_one():
    m = _bridge()
    a = m.Alpha(ZSCORE, max_contracts=4, deadband=0.0)
    assert a._round(0.5, 0) == 2
    assert a._round(-1.0, 0) == -4
    with pytest.raises(ValueError, match="max_contracts"):
        m.Alpha(ZSCORE, max_contracts=0)


def test_a_deadband_outside_one_contract_is_refused():
    m = _bridge()
    with pytest.raises(ValueError, match="deadband"):
        m.Alpha(ZSCORE, deadband=1.0)


# ----------------------------------------------------------- the boundary
def test_the_bridge_does_not_import_the_engine():
    """It is loaded by file path under a bare name precisely so that importing
    it cannot execute `src/core/engine/__init__.py`."""
    import subprocess

    code = (
        "import importlib.util, sys; sys.path.insert(0, '.');"
        "spec = importlib.util.spec_from_file_location("
        "'harness_alpha', 'src/core/engine/remote/alpha_env.py');"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "print([x for x in sys.modules if x.startswith('src.core')])"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", (
        f"the bridge pulled src.core into the strategy process: {out.stdout}")


def test_the_child_installs_the_bridge_before_the_strategy_loads():
    """So a strategy may `import harness_alpha` at its own top level."""
    child = (REPO_ROOT / "src/core/engine/remote/child.py").read_text()
    install = child.index('sys.modules["harness_alpha"]')
    load = child.index("spec.loader.exec_module(module)", install)
    assert install < load, "harness_alpha is installed after the strategy runs"
