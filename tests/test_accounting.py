"""No engine without a ledger row, and no free evaluation.

Two Task 23 properties, both structural rather than conventional:

1. `Engine(...)` requires a `RunTicket`, and there are exactly three ways to mint
   one. This file walks the tree for construction sites so a fourth route cannot
   appear quietly -- which is the failure the rule exists to stop: a legitimate
   entry point added without a thought for the ledger.
2. The agent's check reads the source and never runs it, so its answer cannot be
   a function of market data and cannot be binary-searched for a score.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.core.checks import static_check, type_check  # noqa: E402
from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.runlog import LedgerRun, RunNotAccounted, RunTicket  # noqa: E402
from src.core.types import Split  # noqa: E402

STRATEGY_PATH = REPO_ROOT / "src" / "strategies" / "current.py"

# Where the engine may be constructed, and with which ticket. Anything else is a
# new route and has to be argued for rather than merged.
LEDGER_SITES = {"run.py", "scripts/nq_smoke.py", "scripts/replay.py"}
CALIBRATION_SITES = {
    "src/core/scoring/calibration/null.py",
    "src/core/scoring/calibration/_sweep.py",
    # Task 31's per-candidate null and the profile-indexed table. Both run
    # generated strategies, so both use `for_null_calibration`, which does not
    # check a digest and in exchange requires the process boundary -- asserted
    # separately by `test_null_calibration_always_pairs_with_the_process_boundary`.
    "src/core/scoring/randomization/candidate.py",
    "scripts/tabulate_profile_null.py",
    # Task 36's campaign driver. It builds an Engine twice: once un-rotated, to
    # recover the session returns the ledger row does not carry and to check
    # that a trial reproduces from its stored source, and once per rotation for
    # the per-candidate null. Both run agent-written strategies, so both take
    # `for_null_calibration` and both go through `RemoteStrategy`. The trials
    # themselves are not here -- those are `run.py`, via `run_trial`.
    "scripts/llm_campaign.py",
}


def ds(**kw) -> SyntheticDataset:
    base = dict(n_sessions=40, ticks_per_session=120, vol_per_tick=0.15, seed=1)
    base.update(kw)
    return SyntheticDataset(SyntheticSpec(**base))


class Base:
    def setup(self, ctx):
        ctx.subscribe("1m")

    def on_bars(self, ctx):
        pass

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass


def python_files() -> list[Path]:
    out = []
    for pattern in ("src/**/*.py", "scripts/*.py", "run.py"):
        out.extend(REPO_ROOT.glob(pattern))
    return sorted(p for p in out if p.is_file())


def engine_construction_sites(path: Path) -> list[tuple[int, str]]:
    """`(line, the keyword names passed)` for every `Engine(...)` call."""
    tree = ast.parse(path.read_text(), filename=str(path))
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else "")
        if name == "Engine":
            sites.append((node.lineno, sorted(k.arg or "**" for k in node.keywords)))
    return sites


# --------------------------------------------------------------------------- #
# 1. no engine without a ticket
# --------------------------------------------------------------------------- #
def test_the_engine_refuses_to_be_built_without_a_ticket():
    with pytest.raises(TypeError, match="ticket"):
        Engine(ds(), Base())
    with pytest.raises(RunNotAccounted, match="RunTicket"):
        Engine(ds(), Base(), ticket="a string that is not a ticket")


def test_a_ticket_cannot_be_conjured_by_calling_the_dataclass():
    """Not a defence against an attacker in this process -- nothing in-process
    is -- but the difference between a rule and a habit."""
    with pytest.raises(RunNotAccounted, match="minted by LedgerRun"):
        RunTicket(kind="ledger")
    with pytest.raises(RunNotAccounted, match="minted by LedgerRun"):
        RunTicket("ledger", "pretending", None)


def test_every_engine_in_the_tree_goes_through_a_named_route():
    """The check that catches the entry point nobody thought about. It walks
    `src/`, `scripts/` and `run.py` -- everything that is not a test."""
    unknown = {}
    for path in python_files():
        rel = str(path.relative_to(REPO_ROOT))
        if rel == "src/core/engine/engine.py":
            continue                      # the class itself, and `for_test`
        for line, keywords in engine_construction_sites(path):
            if "ticket" not in keywords:
                unknown.setdefault(rel, []).append(
                    f"line {line}: Engine(...) with no ticket= argument"
                )
            elif rel not in LEDGER_SITES | CALIBRATION_SITES:
                unknown.setdefault(rel, []).append(
                    f"line {line}: a new file constructs an Engine; say which "
                    f"route it uses in tests/test_accounting.py"
                )
    assert not unknown, (
        "engine construction outside the named routes:\n  "
        + "\n  ".join(f"{k}: {v}" for k, v in unknown.items())
    )


def test_the_named_routes_all_still_construct_something():
    """A guard on the guard: the list above passes vacuously if a route stops
    building engines, and then it is documentation of a thing that used to be."""
    for rel in LEDGER_SITES | CALIBRATION_SITES:
        sites = engine_construction_sites(REPO_ROOT / rel)
        assert sites, f"{rel} is a named engine route and constructs no engine"


def calls_named(path: Path, names: set[str]) -> list[tuple[int, str]]:
    """Call sites, by AST rather than by substring.

    A docstring that *names* `for_test` is not a use of it, and a check that
    cannot tell the two apart makes the prose in this repo unwritable.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else "")
        if name in names:
            out.append((node.lineno, name))
    return out


def test_null_calibration_always_pairs_with_the_process_boundary():
    """`for_null_calibration` runs arbitrary generated strategies, so it does not
    check a digest -- and in exchange it must never run them in process. Findings
    2 and 3 would otherwise reopen once per candidate."""
    offenders = {}
    for path in python_files():
        text = path.read_text()
        if "for_null_calibration" not in text:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        if rel == "src/core/runlog.py":
            continue                          # where it is defined
        if "RemoteStrategy" not in text:
            offenders[rel] = "mints a null-calibration ticket with no RemoteStrategy"
    assert not offenders, offenders


def test_the_test_only_constructor_is_used_by_no_production_file():
    """The brief's requirement, and the reason `for_test` is a separate name
    rather than an optional argument: an optional argument is one a production
    caller can also omit."""
    offenders = {}
    for path in python_files():
        rel = str(path.relative_to(REPO_ROOT))
        if rel in ("src/core/engine/engine.py", "src/core/runlog.py"):
            continue                      # where it is defined
        sites = calls_named(path, {"for_test"})
        if sites:
            offenders[rel] = sites
    assert not offenders, f"Engine.for_test is called in production code: {offenders}"


def test_a_ledger_run_writes_a_row_even_when_the_body_raises(tmp_path):
    """The property the context manager exists for. Five append calls in five
    branches were correct and were correct by everyone remembering."""
    led = tmp_path / "ledger.jsonl"
    with pytest.raises(RuntimeError, match="deliberate"):
        with LedgerRun(led, row_builder=lambda r: {**r, "built": True}) as run_log:
            run_log.update({"outcome": "crashed"})
            raise RuntimeError("deliberate")
    lines = [line for line in led.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    assert '"outcome": "crashed"' in lines[0] and '"built": true' in lines[0]


def test_a_ledger_run_writes_a_row_even_when_nothing_updated_it(tmp_path):
    """A run that told the block nothing still happened."""
    led = tmp_path / "ledger.jsonl"
    with LedgerRun(led, row_builder=lambda r: {**r, "empty": True}) as run_log:
        assert run_log.ticket.kind == "ledger"
    assert led.read_text().strip() == '{"empty": true}'


def test_the_ticket_does_not_outlive_the_block(tmp_path):
    led = tmp_path / "ledger.jsonl"
    run_log = LedgerRun(led, row_builder=dict)
    with pytest.raises(RunNotAccounted, match="only inside the"):
        run_log.ticket


def test_calibration_needs_a_reason():
    with pytest.raises(RunNotAccounted, match="needs a reason"):
        RunTicket.for_calibration("")
    assert RunTicket.for_calibration("null tabulation").kind == "calibration"


# --------------------------------------------------------------------------- #
# 2. the free check executes nothing
# --------------------------------------------------------------------------- #
EXECUTION = {"import_module", "exec_module", "spec_from_file_location", "Engine",
             "exec", "eval", "run_path", "run_module", "__import__"}


@pytest.mark.parametrize("rel", [
    "scripts/agent_check.py",
    "src/core/checks/static.py",
    "src/core/checks/typecheck.py",
    "src/core/checks/__init__.py",
])
def test_the_check_never_imports_or_executes_the_strategy(rel):
    """Read as source, not run. `import` counts as running: a module's top level
    executes on import, and a probe would simply live there.

    `compile()` is allowed and is the point -- it produces a code object and does
    not run it. So is mypy, which parses and infers.
    """
    sites = calls_named(REPO_ROOT / rel, EXECUTION)
    assert not sites, f"{rel} can execute strategy code at {sites}"


def test_the_check_passes_the_committed_strategy():
    report = static_check(STRATEGY_PATH)
    assert report.ok, [str(f) for f in report.findings]
    assert type_check(STRATEGY_PATH, report.strategy_class).ok


@pytest.mark.parametrize("source,code", [
    ("def broken(:\n    pass\n", "E002"),
    ("import os\nos.getcwd()\n\nclass Strategy:\n"
     "    def setup(self, ctx): ctx.subscribe('1m')\n"
     "    def on_bars(self, ctx): pass\n"
     "    def on_fill(self, ctx): pass\n"
     "    def on_lifecycle(self, ctx): pass\n", "E011"),
    ("class Strategy:\n"
     "    def setup(self, ctx): ctx.subscribe('1m')\n"
     "    def on_bars(self, ctx): pass\n"
     "    def on_fill(self, ctx): pass\n", "E030"),
    ("class Strategy:\n"
     "    def setup(self, ev, ctx): ctx.subscribe('1m')\n"
     "    def on_bars(self, ctx): pass\n"
     "    def on_fill(self, ctx): pass\n"
     "    def on_lifecycle(self, ctx): pass\n", "E031"),
    ("class Strategy:\n"
     "    def setup(self, ctx): return ['1m']\n"
     "    def on_bars(self, ctx): pass\n"
     "    def on_fill(self, ctx): pass\n"
     "    def on_lifecycle(self, ctx): pass\n", "E040"),
    ("class Nothing:\n    pass\n", "E020"),
    ("class Strategy:\n"
     "    def setup(self, ctx): ctx.subscribe('tick')\n"
     "    def on_bars(self, ctx): pass\n"
     "    def on_fill(self, ctx): pass\n"
     "    def on_lifecycle(self, ctx): pass\n", "E051"),
])
def test_the_static_check_catches(source, code, tmp_path):
    path = tmp_path / "current.py"
    path.write_text(source)
    report = static_check(path, source)
    assert code in {f.code for f in report.findings}, [str(f) for f in report.findings]


def test_a_module_level_call_is_the_case_that_matters(tmp_path):
    """The probe would not bother with a callback. `import subprocess` at module
    level is fine; calling it there runs inside the harness process at import,
    before any of the rest of this exists."""
    path = tmp_path / "current.py"
    path.write_text(
        "import subprocess\n"
        "LEAK = subprocess.run(['echo', 'hi'])\n"
        "class Strategy:\n"
        "    def setup(self, ctx): ctx.subscribe('1m')\n"
        "    def on_bars(self, ctx): pass\n"
        "    def on_fill(self, ctx): pass\n"
        "    def on_lifecycle(self, ctx): pass\n"
    )
    report = static_check(path)
    assert "E011" in {f.code for f in report.findings}


def test_the_two_layers_catch_different_things(tmp_path):
    """The AST layer sees a missing callback the type checker would not look for;
    the type checker sees a constructor the harness cannot call and a return type
    the Protocol forbids. Neither alone is the check."""
    path = tmp_path / "current.py"
    path.write_text(
        "class Strategy:\n"
        "    def __init__(self, interval: str): self.interval = interval\n"
        "    def setup(self, ctx) -> None: ctx.subscribe(self.interval)\n"
        "    def on_bars(self, ctx) -> int: return 1\n"
        "    def on_fill(self, ctx) -> None: pass\n"
        "    def on_lifecycle(self, ctx) -> None: pass\n"
    )
    assert static_check(path).ok, "the AST layer was supposed to miss this one"


def test_the_check_reports_no_number_and_no_market_data(tmp_path):
    r = subprocess.run(
        [sys.executable, "scripts/agent_check.py",
         "--ledger", str(tmp_path / "ledger.jsonl")],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert r.returncode == 0, r.stderr
    lowered = r.stdout.lower()
    for forbidden in ("sortino", "sharpe", "significance", "pnl", "session",
                      "fill", "blotter"):
        assert forbidden not in lowered, r.stdout


def test_the_oracle_answer_does_not_depend_on_the_threshold(tmp_path):
    """Finding 6, as a regression test.

    The attack wrote a strategy that raises when its own P&L crosses a threshold
    and read the exit code. The full demonstration is
    `docs/reviews/task23/oracle_after.py`; this is the cheap in-process version:
    the same file at three thresholds decades apart has to produce byte-identical
    findings, because the findings are a property of the text.
    """
    template = (
        "class Strategy:\n"
        "    THRESHOLD = {t!r}\n"
        "    def setup(self, ctx): ctx.subscribe('5m')\n"
        "    def on_bars(self, ctx): pass\n"
        "    def on_fill(self, ctx):\n"
        "        if ctx.realized_pnl > self.THRESHOLD:\n"
        "            raise RuntimeError('oracle')\n"
        "    def on_lifecycle(self, ctx): pass\n"
    )
    answers = set()
    for threshold in (-1e9, 0.0, 1e9):
        path = tmp_path / f"s{threshold}.py"
        source = template.format(t=threshold)
        path.write_text(source)
        report = static_check(path, source)
        answers.add((report.ok, tuple(str(f) for f in report.findings)))
    assert len(answers) == 1, answers
