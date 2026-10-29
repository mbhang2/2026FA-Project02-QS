"""Does the agent's file look like a strategy? Answered without running it.

`scripts/agent_check.py` used to compile the strategy, wire it to the harness and
walk a small dataset. It reported pass or fail and no number, on the reasoning
that a check yielding no score cannot be used for selection.

Adversarial review's finding 6 is that pass/fail *is* a number if you ask enough
times. A strategy that says

    def on_fill(self, ctx):
        if ctx.realized_pnl > THRESHOLD:
            raise RuntimeError()

turns the exit code into one bit of its own P&L, and a binary search over
THRESHOLD across repeated free checks recovers the rest. Eighteen probes
recovered peak realized P&L to +/-0.0002 with **zero trials counted**. It does
not forge a trial; it performs trials the system has agreed not to look at, which
is the cleanest possible attack on honest N.

**Narrowing the channel is not the fix.** Any output that depends on market data
can carry a score, so rate-limiting or coarsening the answer reduces bandwidth
and leaves the mechanism. The output has to be *incapable* of depending on market
data, and the only way to arrange that is for no market data to be involved:
the check reads the source text and never runs it.

That rules out `import`, which is the part worth saying out loud. A module's top
level executes on import, so `importlib.import_module("src.strategies.current")`
is execution -- and a strategy could do its whole probe at module scope. Every
check here works on the parse tree.

What it cannot see is anything that only happens at run time: a `TypeError` on
the first bar, an unsubscribed id, an order the engine rejects. Those now cost a
trial, and that is the intended behaviour rather than a regression. A free crash
check is a mechanism for making crashes not count, which is the same hole in a
friendlier shape.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

# The callbacks `src.core.types.Strategy` declares, and the engine calls. Checked
# against that Protocol by a test rather than restated by hand here -- a list
# that drifts from the Protocol is a check that passes the wrong file.
REQUIRED_CALLBACKS = ("setup", "on_bars", "on_fill", "on_lifecycle")
OPTIONAL_CALLBACKS = ("on_tick", "on_quote")

# What `run.load_strategy()` looks for, in its order.
STRATEGY_NAMES = ("STRATEGY", "Strategy", "MomentumStrategy")

# Statements that may appear at module level. Everything else does work at import
# time, and import time is inside the harness process, before any of this ran.
_INERT_AT_MODULE_LEVEL = (
    ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef,
    ast.AsyncFunctionDef, ast.Assign, ast.AnnAssign, ast.Expr, ast.Pass,
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One reason the file is not acceptable. `line` is 0 when there is no one
    place to point at -- a missing class, for instance."""

    code: str
    message: str
    line: int = 0

    def __str__(self) -> str:
        where = f":{self.line}" if self.line else ""
        return f"{self.code}{where}  {self.message}"


@dataclass
class StaticReport:
    path: Path
    findings: list[Finding] = field(default_factory=list)
    strategy_class: str = ""
    callbacks: tuple[str, ...] = ()
    subscribes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.findings

    def add(self, code: str, message: str, line: int = 0) -> None:
        self.findings.append(Finding(code, message, line))


def _is_docstring(node: ast.stmt, index: int) -> bool:
    return (
        index == 0
        and isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _is_dunder_main_guard(node: ast.stmt) -> bool:
    return isinstance(node, ast.If) and ast.unparse(node.test).startswith("__name__")


def _callable_at_module_level(node: ast.stmt) -> bool:
    """Does this module-level statement evaluate a call?

    A call is the shape that does work. `X = 3` is inert; `X = probe()` is not,
    and `probe()` on its own line is the whole attack with the class definition
    left as decoration.
    """
    if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef,
                         ast.FunctionDef, ast.AsyncFunctionDef, ast.Pass)):
        return False
    return any(isinstance(sub, ast.Call) for sub in ast.walk(node))


def _signature_of(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = fn.args
    return [a.arg for a in (*args.posonlyargs, *args.args)]


def _collect_subscriptions(tree: ast.AST) -> tuple[str, ...]:
    """String literals passed to something called `subscribe`.

    Best effort and known to be: a computed interval is invisible here. It is
    reported rather than enforced -- the engine is the authority on whether a
    subscription is valid, and it will say so at run time.
    """
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "subscribe"):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    out.append(arg.value)
    return tuple(dict.fromkeys(out))


def static_check(path: Path, source: str | None = None) -> StaticReport:
    """Compile, parse and inspect. Never import, never execute."""
    report = StaticReport(path=path)
    try:
        text = source if source is not None else path.read_text()
    except OSError as exc:
        report.add("E001", f"cannot read {path}: {exc}")
        return report

    # 1. Compile. Catches syntax errors and nothing else runs.
    try:
        compile(text, str(path), "exec")
    except SyntaxError as exc:
        report.add("E002", f"syntax error: {exc.msg}", exc.lineno or 0)
        return report
    tree = ast.parse(text, filename=str(path))

    # 2. Module level must be inert. Anything that evaluates a call runs inside
    #    the harness process at import, before the engine exists -- which is
    #    where a probe would put itself if this check only looked at the class.
    for index, node in enumerate(tree.body):
        if _is_docstring(node, index) or _is_dunder_main_guard(node):
            continue
        if not isinstance(node, _INERT_AT_MODULE_LEVEL):
            report.add(
                "E010",
                f"{type(node).__name__} at module level does work at import time; "
                "put it inside a callback",
                node.lineno,
            )
        elif _callable_at_module_level(node):
            report.add(
                "E011",
                "a call at module level runs when the harness imports this file, "
                "before the engine exists; move it into setup()",
                node.lineno,
            )

    # 3. Something the loader will find.
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    assigned = {
        t.id
        for n in tree.body if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    }
    chosen = next(
        (name for name in STRATEGY_NAMES if name in classes or name in assigned),
        "",
    )
    if not chosen:
        report.add(
            "E020",
            "no strategy the harness can load: define a class named Strategy, "
            f"or expose an instance as STRATEGY (looked for {list(STRATEGY_NAMES)})",
        )
        return report
    report.strategy_class = chosen
    if chosen not in classes:
        # `STRATEGY = SomeClass()` -- follow it to the class if it is in this file
        report.add(
            "I001",
            f"{chosen} is assigned rather than defined here; its callbacks cannot "
            "be checked statically",
        ) if chosen not in classes else None
        return report

    node = classes[chosen]

    # 4. The callbacks, with the signature the engine calls them with.
    methods = {
        m.name: m for m in node.body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    inherits = [ast.unparse(b) for b in node.bases]
    report.callbacks = tuple(sorted(methods))

    for name in REQUIRED_CALLBACKS:
        if name not in methods:
            if inherits:
                report.add(
                    "I002",
                    f"{chosen} does not define {name}(); it may inherit it from "
                    f"{', '.join(inherits)}, which this check cannot follow",
                )
                continue
            report.add("E030", f"{chosen} does not define {name}(self, ctx)")

    for name in (*REQUIRED_CALLBACKS, *OPTIONAL_CALLBACKS):
        method = methods.get(name)
        if method is None:
            continue
        params = _signature_of(method)
        if params[:1] != ["self"] or len(params) != 2:
            report.add(
                "E031",
                f"{name}{tuple(params)} takes the context and nothing else: "
                f"def {name}(self, ctx). The engine calls it with one argument.",
                method.lineno,
            )
        if isinstance(method, ast.AsyncFunctionDef):
            report.add(
                "E032",
                f"{name}() is async; the engine calls callbacks synchronously",
                method.lineno,
            )

    # 5. The pre-Task-5 shape, which the engine refuses at run time. Named here
    #    because "returns a list of actions" is the single most likely thing for
    #    a model to write from an older idea of what a backtester looks like.
    setup = methods.get("setup")
    if setup is not None:
        for sub in ast.walk(setup):
            if isinstance(sub, ast.Return) and sub.value is not None:
                report.add(
                    "E040",
                    "setup() must not return subscriptions; call ctx.subscribe() "
                    "and return None",
                    sub.lineno,
                )
                break

    report.subscribes = _collect_subscriptions(tree)
    if not report.subscribes and "subscribe" not in ast.unparse(tree):
        report.add(
            "E050",
            "nothing calls ctx.subscribe(); a strategy that subscribes to "
            "nothing is never called back",
        )
    if "tick" in report.subscribes and "on_tick" not in methods:
        report.add(
            "E051",
            "subscribes to ticks but does not define on_tick(self, ctx)",
        )
    if any(s in ("quote", "quotes") for s in report.subscribes) and "on_quote" not in methods:
        report.add(
            "E052",
            "subscribes to quotes but does not define on_quote(self, ctx)",
        )
    return report
