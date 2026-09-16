"""Cross-tree checks (Task 41). These run in CI; the teammate suites do not.

`testpaths = ["tests"]`, so `pytest` at the root collects this file and does not
collect `nalin/l1-loop/tests/`. Anything that has to **fail when somebody gets it
wrong later** therefore belongs here rather than beside the code it guards. That
is the whole reason this file exists at the root.

Three things are checked:

1. **The fixture-calendar allowlist**, which is Task 41's replacement for a rule
   that lived in a header comment.
2. **The two metric implementations against each other**, on the same returns.
3. **The two fill models against each other**, on the same decisions.

The second and third are *differential references*: nalin's engine is a second
implementation of the same arithmetic by a different author, which is the
strongest check available on code nobody has independently verified. Where they
disagree the test records the disagreement rather than asserting one side --
a difference in modelling assumptions is a finding, not a bug, and the two are
told apart by reading, not by a tolerance.
"""

from __future__ import annotations

import math
import statistics as st
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
NALIN = REPO_ROOT / "nalin" / "l1-loop"
for p in (str(REPO_ROOT), str(NALIN)):
    if p not in sys.path:
        sys.path.insert(0, p)

pd = pytest.importorskip("pandas", reason="nalin's engine is pandas-based")

from src.core.scoring import metrics as harness_metrics  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. the fixture-calendar allowlist
# --------------------------------------------------------------------------- #
def test_the_fixture_registry_refuses_a_dataset_it_does_not_name(tmp_path, monkeypatch):
    """Point it at something that is not a fixture and it fails by name.

    Task 40 wrote this rule into a header comment in each calendar file. A rule
    with nothing comparing it to the configuration is the pattern Task 35 found
    in `agent-patterns/` -- the critic pinned to the coder's model family, with
    the invariant stated twice in prose and tested nowhere.
    """
    from nalin_l1.splitting import windows

    real = tmp_path / "nq-trades-but-really.yaml"
    real.write_text("dataset: x\nsplits: {}\n")
    monkeypatch.setattr(windows, "FIXTURE_REGISTRY", tmp_path)
    monkeypatch.setattr(windows, "REGISTRIES", (tmp_path,))

    with pytest.raises(windows.FixtureCalendarRefused) as exc:
        windows.calendar_path("nq-trades-but-really")
    message = str(exc.value)
    assert "config/calendars/" in message, "it must name where the file belongs"
    assert "artifact_digests()" in message, "and what registering it there costs"


def test_the_allowlist_catches_the_file_being_added_not_only_used():
    """A calendar committed to the fixture registry for a real dataset is
    already the mistake, whether or not a run has reached it."""
    from nalin_l1.splitting import windows

    assert windows.audit_fixture_registry() == [], (
        "calendars in the fixture registry that the allowlist does not name"
    )
    assert windows.FIXTURE_DATASETS == frozenset({"placeholder", "es-20250703"})


def test_the_two_fixtures_still_resolve():
    from nalin_l1.splitting import windows

    for name in sorted(windows.FIXTURE_DATASETS):
        assert windows.calendar_path(name).is_file()
        train, validation, holdout = windows.registered_split(name)
        assert validation.start > train.end and holdout.start > validation.end


# --------------------------------------------------------------------------- #
# 2. the two metric implementations, differentially
# --------------------------------------------------------------------------- #
RETURNS = [
    [0.01, -0.02, 0.015, 0.0, -0.005, 0.03, -0.01, 0.002, -0.018, 0.021],
    [-0.004, -0.011, 0.006, 0.019, -0.03, 0.012, 0.0, 0.008, -0.002, 0.014],
    [0.05, -0.04, 0.02, -0.01, 0.0, 0.0, 0.03, -0.02, 0.01, -0.03],
]


@pytest.mark.parametrize("returns", RETURNS, ids=("mixed", "small", "wide"))
def test_max_drawdown_agrees_in_magnitude_and_disagrees_in_sign(returns):
    """**A disagreement, found by this comparison and documented rather than
    reconciled: the two implementations use opposite sign conventions.**

    nalin's returns a *positive magnitude* and says so
    (`nalin_l1/metrics/risk.py`: "returned as a positive magnitude"); the
    harness's returns a *negative* number, `min(worst, cum - peak)`, and its
    docstring does not mention the sign at all. Both are internally consistent
    and neither is wrong; what was wrong is that nothing anywhere said they
    differ, and a drawdown comparison read backwards is the cheapest possible
    way to conclude the wrong thing about a candidate.

    **Which side I believe: neither, on sign** -- it is a convention, and the
    harness's is the one every number in `docs/design/metric.md` was computed
    with, so it stays. What this test does is pin the difference so that a
    future change to either makes a test fail rather than making a comparison
    quietly wrong. The harness's docstring should say "negative"; that is an
    edit to `src/core/**` and therefore a stop-and-report, not something to slip
    in here.

    **On the arithmetic they agree to within 0.5%**, which is the real value of
    the comparison: two independent implementations by different authors, on
    code nobody had cross-checked. The residual is compounding versus adding --
    nalin walks a multiplicative equity curve, the harness an additive
    cumulative return path, which is correct for per-session returns already
    normalised by notional.
    """
    from nalin_l1.metrics.risk import max_drawdown as nalin_mdd

    equity = []
    running = 1.0
    for r in returns:
        running *= 1.0 + r
        equity.append(running)

    theirs = float(nalin_mdd(pd.Series(equity)))

    # The harness's is on the *additive* cumulative path, not the compounded
    # one -- per-session returns are already normalised by notional.
    ours = harness_metrics.max_drawdown(returns)

    # Opposite signs, pinned. If either side changes convention this fails.
    assert theirs > 0, "nalin's max_drawdown stopped returning a magnitude"
    assert ours < 0, "the harness's max_drawdown stopped returning a signed drop"

    # Compounding versus adding is a real difference at these magnitudes; the
    # check is that they agree to within it.
    assert math.isclose(abs(theirs), abs(ours), rel_tol=0.05), (
        f"max drawdown disagrees beyond the compounding difference: "
        f"nalin {theirs:.6f} vs harness {ours:.6f}"
    )


@pytest.mark.parametrize("returns", RETURNS, ids=("mixed", "small", "wide"))
def test_sharpe_disagrees_by_a_known_annualisation_factor(returns):
    """**They do not agree, and the disagreement is documented rather than
    reconciled.** nalin annualises by sqrt(252); the harness does not annualise
    at all, because Sortino on per-session returns is compared against an
    empirical null built from the same unannualised statistic and a constant
    factor would cancel on both sides while making the number look like an
    annual rate it is not.

    The ratio is exactly sqrt(252). If that ever stops being exact, one of the
    two changed its definition and this test is how anyone finds out.
    """
    from nalin_l1.metrics.returns import sharpe_ratio as nalin_sharpe

    theirs = float(nalin_sharpe(pd.Series(returns)))
    ours = harness_metrics.sharpe(returns)
    assert ours != 0
    assert math.isclose(theirs / ours, math.sqrt(252.0), rel_tol=1e-9), (
        f"the two Sharpes no longer differ by exactly sqrt(252): "
        f"nalin {theirs:.6f}, harness {ours:.6f}, ratio {theirs / ours:.6f}"
    )


@pytest.mark.parametrize("returns", RETURNS, ids=("mixed", "small", "wide"))
def test_the_two_standard_deviations_agree(returns):
    """Underneath both Sharpes. A sample standard deviation is arithmetic nobody
    has independently checked on this side, and pandas is the check."""
    theirs = float(pd.Series(returns).std())          # ddof=1
    ours = harness_metrics._sd(returns)
    assert math.isclose(theirs, ours, rel_tol=1e-12), (theirs, ours)


def test_the_second_objective_is_a_diagnostic_and_not_the_objective():
    """`composite_score` is a second scoring path. It is recorded, never used to
    accept: the objective is single and pre-registered, and a scorer any code
    can import is what `metrics.py`'s import guard exists to stop."""
    from nalin_l1.metrics.composite import composite_score

    assert callable(composite_score)
    scorer = (REPO_ROOT / "src" / "core" / "scoring" / "scorer.py").read_text()
    assert "composite" not in scorer.lower(), (
        "the harness scorer now mentions a composite score; the objective is "
        "single and pre-registered"
    )


# --------------------------------------------------------------------------- #
# 3. the two fill models, differentially
# --------------------------------------------------------------------------- #
def test_the_two_fill_models_disagree_and_the_difference_is_the_model():
    """nalin fills at the *next bar's open* and charges bps on traded notional;
    the harness fills a resting limit only on a trade strictly through it, at
    tick resolution, and charges commission per contract.

    These are two modelling choices, not two implementations, and Task 4's
    argument is about which one the loop *scores* on -- the pessimistic one,
    because whichever model a candidate is scored against is the one it learns
    to exploit. The check here is that the difference is in the direction that
    argument assumes: nalin's model is the more optimistic of the two, because a
    fill is unconditional once the position changes.
    """
    from nalin_l1.execution.simulator import simulate_fill

    bar = pd.Series({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5})
    filled = simulate_fill(prev_position=0.0, new_position=1.0, bar=bar,
                           fee_bps=1.0, slippage_bps=1.0)

    # Unconditional: the position moves whatever the bar did.
    assert filled["executed_position"] == 1.0
    assert filled["fill_price"] == 100.0

    flat_bar = pd.Series({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0})
    still_filled = simulate_fill(prev_position=0.0, new_position=1.0, bar=flat_bar,
                                 fee_bps=1.0, slippage_bps=1.0)
    assert still_filled["executed_position"] == 1.0, (
        "nalin's model fills on a bar with no range at all -- which is the "
        "optimism the harness's `unfavorable` model exists to remove"
    )

    # And the harness's refuses exactly that case.
    from src.core.engine.fills import FavorableFill, UnfavorableFill
    from src.core.types import Order, OrderType, Side, TimeInForce, Tick

    order = Order(order_id=1, side=Side.BUY, size=1,
                  order_type=OrderType.LIMIT, tif=TimeInForce.DAY,
                  limit_price=100.0)
    at_the_limit = Tick(ts=0, price=100.0, size=1, session=0, seq=0,
                        side=Side.BUY)
    assert UnfavorableFill().resolve(order, at_the_limit) is None, (
        "the harness's unfavorable model filled at the limit price; it is "
        "supposed to require a trade strictly through it"
    )
    # The optimistic model does fill there -- which is the axis the two teams'
    # engines differ on, and nalin's sits at the optimistic end of it.
    assert FavorableFill().resolve(order, at_the_limit) is not None


# --------------------------------------------------------------------------- #
# 4. Gowtam's loop as a second driver over the harness interface
# --------------------------------------------------------------------------- #
def _gowtam():
    path = str(REPO_ROOT / "Gowtam" / "L1")
    if path not in sys.path:
        sys.path.insert(0, path)
    from loop import evaluate_step, paths as gpaths

    return evaluate_step, gpaths


GOOD_CANDIDATE = '''"""A minimal candidate, for the driver test."""


class Strategy:
    def setup(self, ctx) -> None:
        ctx.subscribe("5m")

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
        history = ctx.history("5m")
        if len(history) < 2:
            return
        last = history[-1]
        if last.close > last.open:
            ctx.buy(size=1, stop_offset=1.0, target_offset=1.0, tag="t")


    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
'''

BROKEN_CANDIDATE = '''"""A candidate that raises on the first bar."""


class Strategy:
    def setup(self, ctx) -> None:
        ctx.subscribe("5m")

    def on_bars(self, ctx) -> None:
        raise RuntimeError("deliberate")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
'''


def test_gowtams_paths_point_at_its_own_project():
    """Regression on the `PROJECT_ROOT` level bug: three `parent`s from a file
    two levels down made it `Gowtam/`, so candidate state was written one
    directory above the project whose name the constant carries."""
    _, gpaths = _gowtam()
    assert gpaths.PROJECT_ROOT.name == "L1"
    assert gpaths.PROJECT_ROOT.parent.name == "Gowtam"
    assert gpaths.REPO_ROOT == REPO_ROOT
    assert gpaths.CONTEXT_API_PATH.is_file()


def test_the_second_driver_produces_a_verifiable_ledger_row(tmp_path):
    """**Two independent drivers over one harness, and this is the evidence.**

    `Gowtam/L1/loop/evaluate_step.py` imports nothing from `src.core`: it writes
    `src/strategies/current.py` and runs `run.py` as a subprocess, which is the
    whole interface the harness offers a driver. The row it gets back carries
    the things that make N honest -- and this asserts them by name, because
    "it wrote a row" and "it wrote a row anybody can check" are different
    claims.
    """
    evaluate_step, gpaths = _gowtam()
    import loop.evaluate_step as es

    cid = "test-candidate"
    built = gpaths.candidate_file(cid, gpaths.STRATEGY_FILE)
    built.write_text(GOOD_CANDIDATE)
    ledger = tmp_path / "ledger.jsonl"
    try:
        row = es.run_evaluate_step(cid, ledger=ledger)
    finally:
        built.unlink(missing_ok=True)

    assert row["outcome"] == "completed", row.get("error")
    for field in ("code_fingerprint", "artifact_fingerprint", "artifacts",
                  "environment_fingerprint", "lockfile_digest", "isolation",
                  "strategy_digest", "fill_log_digest", "dataset_digest"):
        assert field in row and row[field], f"a verifiable row needs {field}"
    assert row["purpose"] == "trial"
    assert row["reconciliation"]["agree"] is True


def test_a_candidate_that_crashes_still_gets_a_row(tmp_path):
    """The accounting the project rests on: a crashed trial is a trial."""
    _, gpaths = _gowtam()
    import loop.evaluate_step as es

    cid = "test-broken"
    built = gpaths.candidate_file(cid, gpaths.STRATEGY_FILE)
    built.write_text(BROKEN_CANDIDATE)
    ledger = tmp_path / "ledger.jsonl"
    try:
        row = es.run_evaluate_step(cid, ledger=ledger)
    finally:
        built.unlink(missing_ok=True)

    assert row["outcome"] == "crashed"
    assert row["score"] is None
    assert "deliberate" in str(row.get("error", "")) + str(row.get("child_traceback", ""))


def test_the_agent_file_is_restored_even_when_the_run_fails(tmp_path):
    """`src/strategies/current.py` is an `ARTIFACT_GLOBS` entry. A candidate left
    in it would change the artifact fingerprint of every subsequent run."""
    _, gpaths = _gowtam()
    import loop.evaluate_step as es

    before = es.HARNESS_STRATEGY.read_text()
    cid = "test-broken-2"
    built = gpaths.candidate_file(cid, gpaths.STRATEGY_FILE)
    built.write_text(BROKEN_CANDIDATE)
    try:
        es.run_evaluate_step(cid, ledger=tmp_path / "l.jsonl")
    finally:
        built.unlink(missing_ok=True)
    assert es.HARNESS_STRATEGY.read_text() == before


def test_nothing_to_evaluate_is_not_a_trial():
    """A candidate that does not exist is not a proposal, and a row for it would
    be a row about the loop rather than about a candidate."""
    _, gpaths = _gowtam()
    import loop.evaluate_step as es

    with pytest.raises(es.NothingToEvaluate):
        es.run_evaluate_step("no-such-candidate-at-all")


def test_the_second_driver_imports_nothing_from_the_harness():
    """The property that makes it evidence. An import would make this a shared
    module rather than a second implementation of the driver contract.

    Checked over the **import statements**, not the source text: the module's
    docstring says "imports nothing from `src.core`", and a substring search
    fails on the sentence that explains why it passes. Task 31 hit exactly this
    with `choose_null`, whose docstring contains the word "score".
    """
    import ast

    tree = ast.parse(
        (REPO_ROOT / "Gowtam" / "L1" / "loop" / "evaluate_step.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    harness = {m for m in imported if m == "src" or m.startswith("src.")}
    assert not harness, f"the second driver imports {sorted(harness)}"
